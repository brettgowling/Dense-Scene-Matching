# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import math
import torch.nn.functional as F
from torch import nn
import torch
import sys, os

from .backbone_base import Backbone
from .resnet import (
    build_resnet34_backbone,
    build_resnet18_backbone,
    build_resnet_backbone,
)
from .wrapper import *

import pickle as pkl

__all__ = [
    "build_resnet_fpn_backbone",
    "build_retinanet_resnet_fpn_backbone",
    "FPN",
    "build_resnet34_fpn_backbone",
    "build_resnet18_fpn_backbone",
]


class FPN(Backbone):
    """
    This module implements Feature Pyramid Network.
    It creates pyramid features built on top of some input feature maps.
    """

    def __init__(
        self,
        bottom_up,
        in_features,
        out_channels,
        norm="",
        top_block=None,
        fuse_type="sum",
    ):
        """
        Args:
            bottom_up (Backbone): module representing the bottom up subnetwork.
                Must be a subclass of :class:`Backbone`. The multi-scale feature
                maps generated by the bottom up network, and listed in `in_features`,
                are used to generate FPN levels.
            in_features (list[str]): names of the input feature maps coming
                from the backbone to which FPN is attached. For example, if the
                backbone produces ["res2", "res3", "res4"], any *contiguous* sublist
                of these may be used; order must be from high to low resolution.
            out_channels (int): number of channels in the output feature maps.
            norm (str): the normalization to use.
            top_block (nn.Module or None): if provided, an extra operation will
                be performed on the output of the last (smallest resolution)
                FPN output, and the result will extend the result list. The top_block
                further downsamples the feature map. It must have an attribute
                "num_levels", meaning the number of extra FPN levels added by
                this block, and "in_feature", which is a string representing
                its input feature (e.g., p5).
            fuse_type (str): types for fusing the top down features and the lateral
                ones. It can be "sum" (default), which sums up element-wise; or "avg",
                which takes the element-wise mean of the two.
        """
        super(FPN, self).__init__()
        # assert isinstance(bottom_up, Backbone)

        # Feature map strides and channels from the bottom up network (e.g. ResNet)

        in_strides = [bottom_up.out_feature_strides[f] for f in in_features]
        in_channels = [bottom_up.out_feature_channels[f] for f in in_features]

        _assert_strides_are_log2_contiguous(in_strides)
        lateral_convs = []
        output_convs = []
        lateral_names = []
        output_conv_names = []

        use_bias = norm == ""

        for idx, in_channels in enumerate(in_channels):
            lateral_norm = get_norm(norm, out_channels)
            output_norm = get_norm(norm, out_channels)

            lateral_conv = Conv2d(
                in_channels,
                out_channels,
                kernel_size=1,
                bias=use_bias,
                norm=lateral_norm,
            )

            output_conv = Conv2d(
                out_channels,
                out_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=use_bias,
                norm=output_norm,
                # activation=nn.LeakyReLU(0.1)
            )
            # weight_init.c2_xavier_fill(lateral_conv)
            # weight_init.c2_xavier_fill(output_conv)
            stage = int(math.log2(in_strides[idx]))
            self.add_module("fpn_lateral{}".format(stage), lateral_conv)
            self.add_module("fpn_output{}".format(stage), output_conv)

            lateral_convs.append(lateral_conv)
            lateral_names.append("fpn_lateral{}".format(stage))
            output_convs.append(output_conv)
            output_conv_names.append("fpn_output{}".format(stage))

        # Place convs into top-down order (from low to high resolution)
        # to make the top-down computation in forward clearer.
        self.lateral_convs = lateral_convs[::-1]
        self.output_convs = output_convs[::-1]
        self.lateral_names = lateral_names[::-1]
        self.output_conv_names = output_conv_names[::-1]

        self.top_block = top_block
        self.in_features = in_features
        self.bottom_up = bottom_up
        # Return feature names are "p<stage>", like ["p2", "p3", ..., "p6"]
        self._out_feature_strides = {
            "p{}".format(int(math.log2(s))): s for s in in_strides
        }
        # top block output feature maps.
        if self.top_block is not None:
            for s in range(stage, stage + self.top_block.num_levels):
                self._out_feature_strides["p{}".format(s + 1)] = 2 ** (s + 1)

        self._out_features = list(self._out_feature_strides.keys())
        self._out_feature_channels = {k: out_channels for k in self._out_features}
        self._size_divisibility = in_strides[-1]
        assert fuse_type in {"avg", "sum"}
        self._fuse_type = fuse_type

    # def to_dev(self, dev_id):
    #    self.bottom_up.to_dev(dev_id)
    #    for name, m in self.named_modules():
    #        print(name)
    #        m.to(dev_id)

    @property
    def size_divisibility(self):
        return self._size_divisibility

    def forward(self, x):
        """
        Args:
            input (dict[str: Tensor]): mapping feature map name (e.g., "res5") to
                feature map tensor for each feature level in high to low resolution order.

        Returns:
            dict[str: Tensor]:
                mapping from feature map name to FPN feature map tensor
                in high to low resolution order. Returned feature names follow the FPN
                paper convention: "p<stage>", where stage has stride = 2 ** stage e.g.,
                ["p2", "p3", ..., "p6"].
        """
        # Reverse feature maps into top-down order (from low to high resolution)
        bottom_up_features = self.bottom_up(x)

        x = [bottom_up_features[f] for f in self.in_features[::-1]]
        results = []
        prev_features = self.lateral_convs[0](x[0])
        results.append(self.output_convs[0](prev_features))

        for features, lateral_conv_name, output_conv_name in zip(
            x[1:], self.lateral_names[1:], self.output_conv_names[1:]
        ):
            lateral_conv = self._modules[lateral_conv_name]
            output_conv = self._modules[output_conv_name]

            top_down_features = F.interpolate(
                prev_features, scale_factor=2, mode="nearest"
            )
            lateral_features = lateral_conv(features)

            prev_features = lateral_features + top_down_features
            if self._fuse_type == "avg":
                prev_features /= 2
            results.insert(0, output_conv(prev_features))

        if self.top_block is not None:
            top_block_in_feature = bottom_up_features.get(
                self.top_block.in_feature, None
            )
            if top_block_in_feature is None:
                top_block_in_feature = results[
                    self._out_features.index(self.top_block.in_feature)
                ]
            results.extend(self.top_block(top_block_in_feature))
        assert len(self._out_features) == len(results)
        outputs = dict(zip(self._out_features, results))
        outputs["res2"] = bottom_up_features["res2"]
        return outputs

    def output_shape(self):
        return {
            name: ShapeSpec(
                channels=self._out_feature_channels[name],
                stride=self._out_feature_strides[name],
            )
            for name in self._out_features
        }

    def load_pretrain(self, model_path):
        if not os.path.exists(model_path):
            print("=> no checkpoint found at '{}'".format(model_path))
        checkpoint = pkl.load(open(model_path, "rb"))["model"]

        new_checkpoint = {}
        for k, v in checkpoint.items():
            if k.split(".")[0] == "backbone":
                new_checkpoint[k[9:]] = torch.from_numpy(v)
        checkpoint = new_checkpoint
        self.load_state_dict(checkpoint, strict=True)
        ckpt_keys = set(checkpoint.keys())
        own_keys = set(self.state_dict().keys())

        missing_keys = own_keys - ckpt_keys
        remain_keys = ckpt_keys - own_keys

        for k in missing_keys:
            print("missing keys from checkpoint {}: {}".format(model_path, k))

        for k in remain_keys:
            print("remaining keys from model {}: {}".format(model_path, k))
        if len(missing_keys) == 0 and len(remain_keys) == 0:
            print("successfully load model from {}".format(model_path))

    def load_bottom_up_pretrain(self, model_path, cfg=None):
        checkpoint = pkl.load(open(model_path, "rb"))["model"]
        new_checkpoint = {}
        for k, v in checkpoint.items():
            new_checkpoint[k] = torch.from_numpy(v)
        checkpoint = new_checkpoint

        self.bottom_up.load_state_dict(checkpoint, strict=False)

        ckpt_keys = set(checkpoint.keys())
        own_keys = set(self.bottom_up.state_dict().keys())
        # print(own_keys)
        missing_keys = own_keys - ckpt_keys
        remain_keys = ckpt_keys - own_keys
        # print(len(own_keys),len(ckpt_keys))
        for k in missing_keys:
            print("missing keys from checkpoint {}: {}".format(model_path, k))

        for k in remain_keys:
            print("remaining keys from model {}: {}".format(model_path, k))
        if len(missing_keys) == 0 and len(remain_keys) == 0:
            print("successfully load model from {}".format(model_path))


def _assert_strides_are_log2_contiguous(strides):
    """
    Assert that each stride is 2x times its preceding stride, i.e. "contiguous in log2".
    """
    for i, stride in enumerate(strides[1:], 1):
        assert (
            stride == 2 * strides[i - 1]
        ), "Strides {} {} are not log2 contiguous".format(stride, strides[i - 1])


class LastLevelMaxPool(nn.Module):
    """
    This module is used in the original FPN to generate a downsampled
    P6 feature from P5.
    """

    def __init__(self):
        super().__init__()
        self.num_levels = 1
        self.in_feature = "p5"

    def forward(self, x):
        return [F.max_pool2d(x, kernel_size=1, stride=2, padding=0)]


class LastLevelConv(nn.Module):
    """
    This module is used in the original FPN to generate a downsampled
    P6 feature from P5.
    """

    def __init__(self, in_channels):
        super().__init__()
        self.num_levels = 1
        self.in_feature = "p5"
        self.p5 = nn.Conv2d(in_channels, in_channels, 3, 2, 1)

    def forward(self, x):
        return [self.p5(x)]


class LastLevelP6P7(nn.Module):
    """
    This module is used in RetinaNet to generate extra layers, P6 and P7 from
    C5 feature.
    """

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.num_levels = 2
        self.in_feature = "res5"
        self.p6 = nn.Conv2d(in_channels, out_channels, 3, 2, 1)
        self.p7 = nn.Conv2d(out_channels, out_channels, 3, 2, 1)
        # for module in [self.p6, self.p7]:
        #    weight_init.c2_xavier_fill(module)

    def forward(self, c5):
        p6 = self.p6(c5)
        p7 = self.p7(F.relu(p6))
        return [p6, p7]


def build_resnet_fpn_backbone(cfg):
    """
    Args:
        cfg: a detectron2 CfgNode

    Returns:
        backbone (Backbone): backbone module, must be a subclass of :class:`Backbone`.
    """
    input_shape = ShapeSpec(channels=3)
    bottom_up = build_resnet_backbone(cfg)
    in_features = ["res2", "res3", "res4", "res5"]
    out_channels = 256
    backbone = FPN(
        top_block=LastLevelMaxPool(),
        bottom_up=bottom_up,
        in_features=in_features,
        out_channels=out_channels,
        norm="",
        fuse_type="sum",
    )
    return backbone


def build_resnet34_fpn_backbone(input_shape, fpn_channel=64):
    """
    Args:
        cfg: a detectron2 CfgNode

    Returns:
        backbone (Backbone): backbone module, must be a subclass of :class:`Backbone`.
    """
    bottom_up = build_resnet34_backbone(input_shape)
    in_features = ["res2", "res3", "res4", "res5"]
    out_channels = fpn_channel
    backbone = FPN(
        top_block=LastLevelConv(fpn_channel),
        bottom_up=bottom_up,
        in_features=in_features,
        out_channels=out_channels,
        norm="BN",
        fuse_type="sum",
    )
    return backbone


def build_resnet18_fpn_backbone(cfg):
    """
    Args:
        cfg: a detectron2 CfgNode

    Returns:
        backbone (Backbone): backbone module, must be a subclass of :class:`Backbone`.
    """
    bottom_up = build_resnet18_backbone(cfg)
    in_features = ["res2", "res3", "res4", "res5"]
    out_channels = 128
    backbone = FPN(
        top_block=LastLevelConv(128),
        bottom_up=bottom_up,
        in_features=in_features,
        out_channels=out_channels,
        norm="BN",
        fuse_type="sum",
    )
    return backbone


def build_vgg_fpn_backbone(cfg):
    bottom_up = vgg19_bn(pretrained=True, final_avg=False)
    in_features = ["p2", "p3", "p4", "p5"]
    out_channels = 256
    backbone = FPN(
        top_block=LastLevelConv(512),
        bottom_up=bottom_up,
        in_features=in_features,
        out_channels=out_channels,
        norm="",
        fuse_type="sum",
    )
    return backbone


if __name__ == "__main__":
    shape = ShapeSpec(channels=3)
    with torch.cuda.device(0), torch.no_grad():
        fpn = build_resnet_fpn_backbone(shape).cuda()
        fpn.load_pretrain(
            "/home/shitaot/work_dirs/pretrain_model/res50_gn_fpn_AP42.6.pkl"
        )
        img = torch.rand(32, 3, 384, 288).cuda()
        for i in range(10000):
            outs = fpn(img)
            for k, v in outs.items():
                print(k, v.shape)

