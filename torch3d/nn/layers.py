import torch
import torch.nn as nn
from torch3d.nn import functional as F


__all__ = ["EdgeConv", "XConv", "SetAbstraction", "FeaturePropagation"]


class EdgeConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, bias=True):
        super(EdgeConv, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.bias = bias
        self.conv = nn.Sequential(
            nn.Conv2d(self.in_channels * 2, self.out_channels, 1, bias=self.bias),
            nn.BatchNorm2d(self.out_channels),
            nn.LeakyReLU(0.2, inplace=True),
        )

    def forward(self, x):
        batch_size = x.shape[0]
        x = x.permute(0, 2, 1)
        _, index = F.knn(x, x, self.kernel_size)
        views = list(index.shape) + [-1]
        x_hat = F.batched_index_select(x, 1, index.view(batch_size, -1))
        x_hat = x_hat.view(views)
        x = x.unsqueeze(2).repeat(1, 1, self.kernel_size, 1)
        x_hat = x_hat - x
        x = torch.cat([x, x_hat], dim=-1)
        x = x.permute(0, 3, 1, 2)
        x = self.conv(x)
        x = torch.max(x, dim=-1, keepdim=False)[0]
        return x


class XConv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation=1, bias=True):
        super(XConv, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.mid_channels = out_channels // 4
        self.kernel_size = kernel_size
        self.dilation = dilation
        self.bias = bias
        self.mlp = nn.Sequential(
            nn.Conv2d(3, self.mid_channels, 1, bias=self.bias),
            nn.BatchNorm2d(self.mid_channels),
            nn.ReLU(True),
            nn.Conv2d(self.mid_channels, self.mid_channels, 1, bias=self.bias),
            nn.BatchNorm2d(self.mid_channels),
            nn.ReLU(True),
        )
        self.stn = nn.Sequential(
            nn.Conv2d(3, self.kernel_size ** 2, [1, self.kernel_size], bias=self.bias),
            nn.BatchNorm2d(self.kernel_size ** 2),
            nn.ReLU(True),
            nn.Conv2d(self.kernel_size ** 2, self.kernel_size ** 2, 1, bias=self.bias),
            nn.BatchNorm2d(self.kernel_size ** 2),
            nn.ReLU(True),
            nn.Conv2d(self.kernel_size ** 2, self.kernel_size ** 2, 1, bias=self.bias),
        )
        self.conv = nn.Sequential(
            nn.Conv2d(
                self.in_channels + self.mid_channels,
                self.out_channels,
                [1, self.kernel_size],
                bias=self.bias,
            ),
            nn.BatchNorm2d(self.out_channels),
            nn.ReLU(True),
        )

    def forward(self, p, q, x=None):
        batch_size = p.shape[0]
        _, index = F.knn(p, q, self.kernel_size * self.dilation)
        index = index[..., :: self.dilation]
        views = list(index.shape) + [-1]
        p = F.batched_index_select(p, 1, index.view(batch_size, -1))
        p = p.view(views)
        p_hat = p - q.unsqueeze(2)
        p_hat = p_hat.permute(0, 3, 1, 2)
        x_hat = self.mlp(p_hat)
        x_hat = x_hat.permute(0, 2, 3, 1)
        if x is not None:
            x = x.permute(0, 2, 1)
            x = F.batched_index_select(x, 1, index.view(batch_size, -1))
            x = x.view(views)
            x_hat = torch.cat([x_hat, x], dim=-1)
        T = self.stn(p_hat)
        T = T.view(batch_size, self.kernel_size, self.kernel_size, -1)
        T = T.permute(0, 3, 1, 2)
        x_hat = torch.matmul(T, x_hat)
        x = x_hat
        x = x.permute(0, 3, 1, 2)
        x = self.conv(x)
        x = x.squeeze(3)
        return q, x


class SetAbstraction(nn.Module):
    def __init__(self, in_channels, mlp, radius=None, k=None, bias=True):
        super(SetAbstraction, self).__init__()
        self.in_channels = in_channels
        self.radius = radius
        self.k = k
        self.bias = bias
        modules = []
        last_channels = self.in_channels
        for channels in mlp:
            modules.append(nn.Conv2d(last_channels, channels, 1, bias=self.bias))
            modules.append(nn.BatchNorm2d(channels))
            modules.append(nn.ReLU(True))
            last_channels = channels
        self.mlp = nn.Sequential(*modules)
        self.maxpool = nn.MaxPool2d([1, k])

    def forward(self, p, q, x=None):
        batch_size = p.shape[0]
        if self.radius is not None:
            index = F.ball_point(p, q, self.radius, self.k)
            views = list(index.shape) + [-1]
            p = F.batched_index_select(p, 1, index.view(batch_size, -1))
            p = p.view(views)
            p_hat = p - q.unsqueeze(2)
            x_hat = p_hat
        else:
            x_hat = p.unsqueeze(1)
        if x is not None:
            if self.radius is not None:
                x = x.permute(0, 2, 1)
                x = F.batched_index_select(x, 1, index.view(batch_size, -1))
                x = x.view(views)
            else:
                x = x.unsqueeze(1)
            x_hat = torch.cat([x_hat, x], dim=-1)
        x = x_hat.permute(0, 3, 1, 2)
        x = self.mlp(x)
        x = self.maxpool(x).squeeze(3)
        return q, x


class FeaturePropagation(nn.Module):
    def __init__(self, in_channels, mlp, k=3, bias=True):
        super(FeaturePropagation, self).__init__()
        self.in_channels = in_channels
        self.bias = bias
        self.k = k
        modules = []
        last_channels = self.in_channels
        for channels in mlp:
            modules.append(nn.Conv1d(last_channels, channels, 1, bias=self.bias))
            modules.append(nn.BatchNorm1d(channels))
            modules.append(nn.ReLU(True))
            last_channels = channels
        self.mlp = nn.Sequential(*modules)

    def forward(self, p, q, x, y=None):
        sqdist, index = F.knn(p, q, self.k)
        sqdist[sqdist < 1e-10] = 1e-10
        weight = torch.reciprocal(sqdist)
        weight = weight / torch.sum(weight, dim=-1, keepdim=True)
        x = F.point_interpolate(x, index, weight)
        if y is not None:
            x = torch.cat([x, y], dim=1)
        x = self.mlp(x)
        return q, x
