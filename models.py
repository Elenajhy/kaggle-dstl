import attr
import torch
import torch.cuda
import torch.nn as nn
import torch.nn.functional as F


@attr.s(slots=True)
class HyperParams:
    classes = attr.ib(default=list(range(10)))
    net = attr.ib(default='DefaultNet')
    n_channels = attr.ib(default=20)
    total_classes = 10
    thresholds = attr.ib(default=[0.2, 0.3, 0.4, 0.5, 0.6])

    patch_inner = attr.ib(default=64)
    patch_border = attr.ib(default=16)

    augment_rotations = attr.ib(default=1)
    augment_flips = attr.ib(default=1)

    validation_square = attr.ib(default=400)

    dropout_keep_prob = attr.ib(default=0.0)  # TODO
    dice_loss = attr.ib(default=0)

    filters_base = attr.ib(default=32)

    n_epochs = attr.ib(default=30)
    oversample = attr.ib(default=0.0)
    lr = attr.ib(default=0.0001)
    batch_size = attr.ib(default=128)

    @property
    def n_classes(self):
        return len(self.classes)

    def update(self, hps_string: str):
        if hps_string:
            for pair in hps_string.split(','):
                k, v = pair.split('=')
                if k == 'classes':
                    v = [int(x) for x in v.split('-')]
                elif '.' in v:
                    v = float(v)
                elif k != 'net':
                    v = int(v)
                setattr(self, k, v)


class BaseNet(nn.Module):
    def __init__(self, hps: HyperParams):
        super().__init__()
        self.hps = hps
        self.register_buffer('global_step', torch.IntTensor(1).zero_())


class MiniNet(BaseNet):
    def __init__(self, hps):
        super().__init__(hps)
        self.conv1 = nn.Conv2d(hps.n_channels, 4, 1)
        self.conv2 = nn.Conv2d(4, 8, 3, padding=1)
        self.conv3 = nn.Conv2d(8, hps.n_classes, 3, padding=1)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = self.conv3(x)
        b = self.hps.patch_border
        return F.sigmoid(x[:, :, b:-b, b:-b])


class OldNet(BaseNet):
    def __init__(self, hps):
        super().__init__(hps)
        self.conv1 = nn.Conv2d(hps.n_channels, 64, 5, padding=2)
        self.conv2 = nn.Conv2d(64, 64, 5, padding=2)
        self.conv3 = nn.Conv2d(64, 64, 5, padding=2)
        self.conv4 = nn.Conv2d(64, hps.n_classes, 7, padding=3)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = self.conv4(x)
        b = self.hps.patch_border
        return F.sigmoid(x[:, :, b:-b, b:-b])


class SmallNet(BaseNet):
    def __init__(self, hps):
        super().__init__(hps)
        self.conv1 = nn.Conv2d(hps.n_channels, 64, 3, padding=1)
        self.conv2 = nn.Conv2d(64, 64, 3, padding=1)
        self.conv3 = nn.Conv2d(64, 64, 3, padding=1)
        self.conv4 = nn.Conv2d(64, 128, 3, padding=1)
        self.conv5 = nn.Conv2d(128, hps.n_classes, 3, padding=1)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.conv4(x))
        x = self.conv5(x)
        b = self.hps.patch_border
        return F.sigmoid(x[:, :, b:-b, b:-b])


def upsample2d(x):
    # repeat is missing: https://github.com/pytorch/pytorch/issues/440
    # return x.repeat(1, 1, 2, 2)
    x = torch.stack([x[:, :, i // 2, :] for i in range(x.size()[2] * 2)], 2)
    x = torch.stack([x[:, :, :, i // 2] for i in range(x.size()[3] * 2)], 3)
    return x


# UNet:
# http://lmb.informatik.uni-freiburg.de/people/ronneber/u-net/u-net-architecture.png


class SmallUNet(BaseNet):
    def __init__(self, hps):
        super().__init__(hps)
        self.conv1 = nn.Conv2d(hps.n_channels, 32, 3, padding=1)
        self.conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv3 = nn.Conv2d(32, 64, 3, padding=1)
        self.conv4 = nn.Conv2d(64, 64, 3, padding=1)
        self.conv5 = nn.Conv2d(64, 32, 3, padding=1)
        self.conv6 = nn.Conv2d(64, 32, 3, padding=1)
        self.conv7 = nn.Conv2d(32, hps.n_classes, 3, padding=1)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x1 = self.pool(x)
        x1 = F.relu(self.conv3(x1))
        x1 = F.relu(self.conv4(x1))
        x1 = F.relu(self.conv5(x1))
        x1 = upsample2d(x1)
        x = torch.cat([x, x1], 1)
        x = F.relu(self.conv6(x))
        x = self.conv7(x)
        b = self.hps.patch_border
        return F.sigmoid(x[:, :, b:-b, b:-b])


class UNet(BaseNet):
    def __init__(self, hps):
        super().__init__(hps)
        self.pool = nn.MaxPool2d(2, 2)
        conv3 = lambda in_, out: nn.Conv2d(in_, out, 3, padding=1)
        sattr = lambda k, v: setattr(self, k, v)
        b = hps.filters_base
        self.filters = [b, b * 2, b * 4, b * 8, b * 16]
        for i, nf in enumerate(self.filters):
            low_nf = hps.n_channels if i == 0 else self.filters[i - 1]
            # TODO - maybe make it a module?
            sattr('conv_down_{}_1'.format(i), conv3(low_nf, nf))
            sattr('conv_down_{}_2'.format(i), conv3(nf, nf))
            if i != 0:
                sattr('conv_up_{}_1'.format(i), conv3(low_nf + nf, low_nf))
                sattr('conv_up_{}_2'.format(i), conv3(low_nf, low_nf))
        self.conv_final = nn.Conv2d(self.filters[0], hps.n_classes, 1)

    def forward(self, x):
        xs = []
        n = len(self.filters)
        for i in range(n):
            x_out = self.pool(xs[-1]) if xs else x
            x_out = F.relu(getattr(self, 'conv_down_{}_1'.format(i))(x_out))
            x_out = F.relu(getattr(self, 'conv_down_{}_2'.format(i))(x_out))
            xs.append(x_out)

        x_out = xs[-1]
        for i in range(n - 1, 0, -1):
            x_skip = xs[i - 1]
            x_out = torch.cat([upsample2d(x_out), x_skip], 1)
            x_out = F.relu(getattr(self, 'conv_up_{}_1'.format(i))(x_out))
            x_out = F.relu(getattr(self, 'conv_up_{}_2'.format(i))(x_out))

        x_out = self.conv_final(x_out)
        b = self.hps.patch_border
        return F.sigmoid(x_out[:, :, b:-b, b:-b])
