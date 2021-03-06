import re
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from torch.hub import load_state_dict_from_url
from torch._six import container_abcs
from ..utils.activations import Swish, sigmoid
from ..utils.arch_utils import make_divisible, round_channels
from ..utils.layers import DropPath
from .base_backbone import Backbone, ClassifierFeature

from typing import List, Tuple, Optional
from copy import deepcopy


_complete_url = lambda x: 'https://github.com/rwightman/pytorch-image-models/releases/download/v0.1-weights/' + x

model_urls = {
    'efficientnet_b0': _complete_url('tf_efficientnet_b0_ns-c0e6a31c.pth'),
    'efficientnet_b1': _complete_url('tf_efficientnet_b1_ns-99dd0c41.pth'),
    'efficientnet_b2': _complete_url('tf_efficientnet_b2_ns-00306e48.pth'),
    'efficientnet_b3': _complete_url('tf_efficientnet_b3_ns-9d44bf68.pth'),
    'efficientnet_b4': _complete_url('tf_efficientnet_b4_ns-d6313a46.pth'),
    'efficientnet_b5': _complete_url('tf_efficientnet_b5_ns-6f26d0cf.pth'),
    'efficientnet_b6': _complete_url('tf_efficientnet_b6_ns-51548356.pth'),
    'efficientnet_b7': _complete_url('tf_efficientnet_b7_ns-1dbc32de.pth'),
    'efficientnet_b8': _complete_url('tf_efficientnet_b8_ra-572d5dd9.pth'),  ## this is actually lower than b5
    'efficientnet_l2': _complete_url('tf_efficientnet_l2_ns-df73bb44.pth'),
    'efficientnet_l2_475': _complete_url('tf_efficientnet_l2_ns_475-bebbd00a.pth'),
    'efficientnet_edge_s': _complete_url('efficientnet_es_ra-f111e99c.pth'),
    'efficientnet_edge_m': _complete_url('tf_efficientnet_em-e78cfe58.pth'),
    'efficientnet_edge_l': _complete_url('tf_efficientnet_el-5143854e.pth'),
    'efficientnet_lite0': _complete_url('tf_efficientnet_lite0-0aa007d2.pth'),
    'efficientnet_lite1': _complete_url('tf_efficientnet_lite1-bde8b488.pth'),
    'efficientnet_lite2': _complete_url('tf_efficientnet_lite2-dcccb7df.pth'),
    'efficientnet_lite3': _complete_url('tf_efficientnet_lite3-b733e338.pth'),
    'efficientnet_lite4': _complete_url('tf_efficientnet_lite4-741542c3.pth'),
}
""" Pretrained model URL
provided by `"rwightman/pytorch-image-models" <https://github.com/rwightman/pytorch-image-models>`_
only take the highest performing weight (if there are multiple weights)
for EfficientNet B0-B7 we use weight trained with NoisyStudent
"""

supported_models = list(model_urls.keys())

TF_BN_MOMENTUM = 1 - 0.99
TF_BN_EPSILON = 1e-3


def get_padding(padding, kernel_size, stride=1, dilation=1):
    dynamic = False
    if isinstance(padding, str):
        padding = padding.lower()
        if padding == 'same':
            # TF compatible 'SAME' padding
            # has a performance and GPU memory allocation overhead in dynamic
            if (stride == 1 and (dilation * (kernel_size - 1)) % 2 == 0):
                # static case, no extra overhead
                padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
            else:
                # dynamic 'SAME' padding, has runtime/GPU memory overhead
                padding = 0
                dynamic = True
        elif padding == 'valid':
            # 'VALID' padding, same as padding=0
            padding = 0
        else:
            # Default to PyTorch style 'same'-ish symmetric padding
            padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
    return padding, dynamic


def create_conv2d(in_channel, out_channel, kernel_size, stride=1, dilation=1, padding='', 
                  bias=True, depthwise=False):
    padding, is_dynamic = get_padding(padding, kernel_size, stride, dilation)
    groups = in_channel if depthwise else 1

    conv_layer = Conv2dSame if is_dynamic else nn.Conv2d
    return conv_layer(in_channel, out_channel, kernel_size, stride, padding=padding, 
        dilation=dilation, groups=groups, bias=bias)


_always_scalar = lambda x: x.item() if isinstance(x, torch.Tensor) else x


def get_same_padding(x: int, k: int, s: int, d: int):
    """ Calculate asymmetric TensorFlow-like 'same' padding for convolution
    """
    # convert to scalar as a workaround for onnx export to make it a constant
    pad = _always_scalar((math.ceil(x / s) - 1) * s + (k - 1) * d + 1 - x)
    return max(pad, 0)


def pad_same(x, k: List[int], s: List[int], d: List[int] = (1, 1)):
    """ Dynamically pad input x with 'same' padding for convolution
    """
    ih, iw = x.shape[-2:]
    pad_h = get_same_padding(ih, k[0], s[0], d[0])
    pad_w = get_same_padding(iw, k[1], s[1], d[1])
    if pad_h > 0 or pad_w > 0:
        x = F.pad(x, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2])
    return x


def conv2d_same(x, weight: torch.Tensor, bias: Optional[torch.Tensor] = None, stride: Tuple[int, int] = (1, 1),
        padding: Tuple[int, int] = (0, 0), dilation: Tuple[int, int] = (1, 1), groups: int = 1):
    x = pad_same(x, weight.shape[-2:], stride, dilation)
    return F.conv2d(x, weight, bias, stride, (0, 0), dilation, groups)


class Conv2dSame(nn.Conv2d):
    """ Tensorflow like 'SAME' convolution wrapper for 2D convolutions
    """

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, bias=True):
        super(Conv2dSame, self).__init__(
            in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)

    def forward(self, x):
        return conv2d_same(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class SqueezeExcite(nn.Module):
    def __init__(self, in_channel, se_ratio=0.25, reduced_base_chs=None,
                 act_layer=nn.ReLU, gate_fn=sigmoid, divisor=1, **_):
        super(SqueezeExcite, self).__init__()
        self.gate_fn = gate_fn
        reduced_chs = make_divisible((reduced_base_chs or in_channel) * se_ratio, divisor)
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv_reduce = nn.Conv2d(in_channel, reduced_chs, 1, bias=True)
        self.act1 = act_layer(inplace=True)
        self.conv_expand = nn.Conv2d(reduced_chs, in_channel, 1, bias=True)

    def forward(self, x):
        x_se = self.avg_pool(x)
        x_se = self.conv_reduce(x_se)
        x_se = self.act1(x_se)
        x_se = self.conv_expand(x_se)
        x = x * self.gate_fn(x_se)
        return x


class DepthwiseSeparableConv(nn.Module):
    """ DepthwiseSeparable block
    
    See Figure 7 on https://arxiv.org/abs/1807.11626
    Used for DS convs in MobileNet-V1 and in the place of IR blocks that have no expansion
    (factor of 1.0). 
    This is an alternative to having a IR with an optional first pw conv.
    """
    def __init__(self, in_channel, out_channel, kernel_size=3, stride=1, se_ratio=0., 
                 pad_type='', act_layer=nn.ReLU, noskip=False, exp_ratio=1.0, 
                 drop_path_rate=0., norm_kwargs=None):
        super(DepthwiseSeparableConv, self).__init__()

        assert kernel_size in [3, 5]
        norm_kwargs = norm_kwargs or {}
        has_se = se_ratio is not None and se_ratio > 0
        self.has_residual = (stride == 1 and in_channel == out_channel) and not noskip

        self.conv_dw = create_conv2d(in_channel, in_channel, kernel_size, stride=stride,
            padding=pad_type, depthwise=True, bias=False)
        self.bn1 = nn.BatchNorm2d(in_channel, **norm_kwargs)
        self.act1 = act_layer(inplace=True)

        # Squeeze-and-excitation
        if has_se:
            self.se = SqueezeExcite(in_channel, se_ratio=se_ratio, reduced_base_chs=in_channel, 
                act_layer=act_layer)
        else:
            self.se = nn.Identity()

        self.conv_pw = create_conv2d(in_channel, out_channel, 1, padding=pad_type, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channel, **norm_kwargs)
        if drop_path_rate > 0:
            self.drop_path = DropPath(drop_path_rate)
        else:
            self.drop_path = nn.Identity()

    def forward(self, x):
        residual = x

        x = self.conv_dw(x)
        x = self.bn1(x)
        x = self.act1(x)

        x = self.se(x)

        x = self.conv_pw(x)
        x = self.bn2(x)

        if self.has_residual:
            x = self.drop_path(x)
            x += residual
        return x


class InvertedResidualBlock(nn.Module):
    """Mobile Inverted Residual Bottleneck Block
    
    See Figure 7 on https://arxiv.org/abs/1807.11626
    Based on MNASNet
    """

    def __init__(self, in_channel, out_channel, kernel_size=3, stride=1, se_ratio=0., 
                 pad_type='', act_layer=nn.ReLU, noskip=False, exp_ratio=1.0, 
                 drop_path_rate=0., norm_kwargs=None):
        super(InvertedResidualBlock, self).__init__()

        assert kernel_size in [3, 5]
        norm_kwargs = norm_kwargs or {}
        has_se = se_ratio is not None and se_ratio > 0
        self.has_residual = (in_channel == out_channel and stride == 1) and not noskip

        ## Point-wise expansion -> _expand_conv in original implementation
        # 'conv_pw' could be by-passed when 'exp_ratio' is 1
        mid_chs = make_divisible(in_channel * exp_ratio)
        self.conv_pw = create_conv2d(in_channel, mid_chs, 1, padding=pad_type, bias=False)
        self.bn1 = nn.BatchNorm2d(mid_chs, **norm_kwargs)
        self.act1 = act_layer(inplace=True)

        # Depth-wise convolution
        self.conv_dw = create_conv2d(mid_chs, mid_chs, kernel_size, stride=stride,
            padding=pad_type, bias=False, depthwise=True)
        self.bn2 = nn.BatchNorm2d(mid_chs, **norm_kwargs)
        self.act2 = act_layer(inplace=True)

        # Squeeze-and-excitation
        if has_se:
            self.se = SqueezeExcite(mid_chs, se_ratio=se_ratio, reduced_base_chs=in_channel, 
                act_layer=act_layer)
        else:
            self.se = nn.Identity()

        # Point-wise linear projection
        self.conv_pwl = create_conv2d(mid_chs, out_channel, 1, padding=pad_type, bias=False)
        self.bn3 = nn.BatchNorm2d(out_channel, **norm_kwargs)
        if drop_path_rate > 0:
            self.drop_path = DropPath(drop_path_rate)
        else:
            self.drop_path = nn.Identity()

    def forward(self, x):
        residual = x

        # Point-wise expansion
        x = self.conv_pw(x)
        x = self.bn1(x)
        x = self.act1(x)

        # Depth-wise convolution
        x = self.conv_dw(x)
        x = self.bn2(x)
        x = self.act2(x)

        # Squeeze-and-excitation
        x = self.se(x)

        # Point-wise linear projection
        x = self.conv_pwl(x)
        x = self.bn3(x)

        if self.has_residual:
            x = self.drop_path(x)
            x += residual
        return x


class EdgeResidual(nn.Module):
    """ Residual block with expansion convolution followed by pointwise-linear w/ stride"""

    def __init__(self, in_channel, out_channel, kernel_size=3, stride=1, se_ratio=0., 
                 pad_type='', act_layer=nn.ReLU, noskip=False, exp_ratio=1.0, 
                 mid_channel=0, drop_path_rate=0., norm_kwargs=None):
        super(EdgeResidual, self).__init__()

        assert kernel_size in [3, 5]
        norm_kwargs = norm_kwargs or {}
        has_se = se_ratio is not None and se_ratio > 0
        self.has_residual = (in_channel == out_channel and stride == 1) and not noskip

        # Expansion convolution
        if mid_channel > 0:
            mid_channel = make_divisible(mid_channel * exp_ratio)
        else:
            mid_channel = make_divisible(in_channel * exp_ratio)
        self.conv_exp = create_conv2d(in_channel, mid_channel, kernel_size, padding=pad_type, bias=False)
        self.bn1 = nn.BatchNorm2d(mid_channel, **norm_kwargs)
        self.act1 = act_layer(inplace=True)

        # Squeeze-and-excitation
        if has_se:
            self.se = SqueezeExcite(mid_channel, se_ratio=se_ratio, reduced_base_chs=in_channel, 
                act_layer=act_layer)
        else:
            self.se = nn.Identity()

        # Point-wise linear projection
        self.conv_pwl = create_conv2d(mid_channel, out_channel, 1, stride=stride, padding=pad_type, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channel, **norm_kwargs)
        if drop_path_rate > 0:
            self.drop_path = DropPath(drop_path_rate)
        else:
            self.drop_path = nn.Identity()

    def forward(self, x):
        residual = x

        # Expansion convolution
        x = self.conv_exp(x)
        x = self.bn1(x)
        x = self.act1(x)

        # Squeeze-and-excitation
        x = self.se(x)

        # Point-wise linear projection
        x = self.conv_pwl(x)
        x = self.bn2(x)

        if self.has_residual:
            x = self.drop_path(x)
            x += residual
        return x


class EfficientNet(nn.Module):
    def __init__(self, block_def, arch_params, global_params, num_classes=1000, in_channel=3,
                 stem_size = 32, fix_stem=False, num_features=None, fix_block_first_last=False,
                 **kwargs):
        super(EfficientNet, self).__init__()
        assert isinstance(global_params, dict)

        self.in_channel = in_channel
        self.arch_params = arch_params
        self.block_def = block_def
        self.global_params = global_params
        self.num_features = self._round_channel(1280) if num_features is None else num_features

        norm_kwargs = global_params['norm_kwargs']
        act_layer = global_params['act_layer']
        pad_type = global_params['pad_type']

        if not fix_stem:
            stem_size = self._round_channel(stem_size)
        self.conv_stem = create_conv2d(in_channel, stem_size, 3, stride=2, 
            padding=pad_type, bias=False)
        self.bn1 = nn.BatchNorm2d(stem_size, **norm_kwargs)
        self.act1 = act_layer(inplace=True)

        self.out_channels = [stem_size]
        self.blocks = self._make_blocks(stem_size, fix_first_last=fix_block_first_last)
        last_channel = self.in_channel

        self.conv_head = create_conv2d(last_channel, self.num_features, 1, padding=pad_type, bias=False)
        self.bn2 = nn.BatchNorm2d(self.num_features, **norm_kwargs)
        self.act2 = act_layer(inplace=True)

        self.global_pool = nn.AdaptiveAvgPool2d(1)
        self.flatten = nn.Flatten(start_dim=1)
        dropout_rate = self.arch_params[3]
        self.dropout = nn.Dropout(p=dropout_rate) if dropout_rate > 0 else nn.Identity()
        self.classifier = nn.Linear(self.num_features, num_classes)

        self.out_channels.extend([self.num_features, num_classes])

        ## TODO: init weight

    
    def _round_channel(self, channel):
        channel_multiplier = self.arch_params[0]
        channel_divisor = self.global_params['channel_divisor']
        channel_min = self.global_params['channel_min']
        return round_channels(channel, channel_multiplier, channel_divisor, channel_min)

    def _decode_block_def(self, fix_first_last=False):
        block_def = self.block_def
        depth_multiplier = self.arch_params[1]
        blocks_args = []
        for idx, stage_strings in enumerate(block_def):
            assert isinstance(stage_strings, container_abcs.Sequence)
            stage_args = [self._decode_str_def(stage_str) for stage_str in stage_strings]
            repeats = [arg.pop('repeat') for arg in stage_args]
            if not (fix_first_last and (idx == 0 or idx == len(block_def)-1)):
                stage_args = self._scale_stage_depth(stage_args, repeats)
            blocks_args.append(stage_args)
        return blocks_args

    def _decode_str_def(self, stage_str):
        assert isinstance(stage_str, str)
        stage_data = stage_str.split('_')
        args_map = {
            'r': ('repeat', int),
            'k': ('kernel_size', int),
            's': ('stride', int),
            'e': ('exp_ratio', float),
            'm': ('mid_channel', int),
            'c': ('out_channel', int),
            'se': ('se_ratio', float)
        }

        args = {'block_type': stage_data[0]}
        noskip = False
        for op in stage_data[1:]:
            if op == 'noskip':
                noskip = True
            else:
                s = re.split(r'(\d.*)', op)
                assert len(s) >= 2
                key, cast = args_map[s[0]]
                args[key] = cast(s[1])
        args['noskip'] = noskip
        assert 'repeat' in args, "stage arguments does not have repeat ('r') argument"
        return args

    def _scale_stage_depth(self, stage_args, repeats):
        depth_multiplier = self.arch_params[1]
        num_repeat = sum(repeats)
        num_repeat_scaled = int(math.ceil(num_repeat * depth_multiplier))
        repeats_scaled = []
        for r in reversed(repeats):
            rs = max(1, round(r/num_repeat * num_repeat_scaled))
            repeats_scaled.append(rs)
            num_repeat -= r
            num_repeat_scaled -= rs
        repeats_scaled = list(reversed(repeats_scaled))

        stage_args_scaled = []
        for sa, rep in zip(stage_args, repeats_scaled):
            stage_args_scaled.extend([deepcopy(sa) for _ in range(rep)])
        return stage_args_scaled

    def _make_blocks(self, in_channel, fix_first_last=False):
        blocks_args = self._decode_block_def(fix_first_last)

        self.in_channel = in_channel
        self.total_layers = sum(len(x) for x in blocks_args)
        self.layer_idx = 0
        blocks = []
        for stage_args in blocks_args:
            assert isinstance(stage_args, list)
            stage = []
            for idx, args in enumerate(stage_args):
                assert args['stride'] in (1, 2)
                if idx > 0:
                    args['stride'] = 1
                layer = self._make_layer(args)
                stage.append(layer)
            blocks.append(nn.Sequential(*stage))
            self.out_channels.append(self.in_channel)
        return nn.Sequential(*blocks)

    def _make_layer(self, layer_args):
        assert isinstance(layer_args, dict)
        block_map = {
            'ds': DepthwiseSeparableConv,
            'ir': InvertedResidualBlock,
            'er': EdgeResidual,
        }
        drop_rate = self.global_params['drop_path_rate'] * self.layer_idx / self.total_layers

        layer_args['in_channel'] = self.in_channel
        layer_args['out_channel'] = self._round_channel(layer_args['out_channel'])
        layer_args['drop_path_rate'] = drop_rate
        layer_args['pad_type'] = self.global_params['pad_type']
        layer_args['norm_kwargs'] = self.global_params['norm_kwargs']
        layer_args['act_layer'] = self.global_params['act_layer']
        if 'mid_channel' in layer_args:
            layer_args['mid_channel'] = self._round_channel(layer_args['mid_channel'])

        block_type = layer_args.pop('block_type')
        layer = block_map[block_type](**layer_args)

        self.in_channel = layer_args['out_channel']
        self.layer_idx += 1
        return layer


    def forward(self, x):
        x = self.conv_stem(x)
        x = self.bn1(x)
        x = self.act1(x)
        x = self.blocks(x)
        x = self.conv_head(x)
        x = self.bn2(x)
        x = self.act2(x)
        x = self.global_pool(x)
        x = self.flatten(x)
        x = self.dropout(x)
        x = self.classifier(x)
        return x

    def get_classifier(self):
        classifier = [self.conv_head, self.bn2, self.act2, self.global_pool,
            self.flatten, self.dropout, self.classifier]
        return nn.Sequential(*classifier)
    
    def reset_classifier(self, num_classes):
        self.classifier = nn.Linear(self.num_features, num_classes)


def _create_model(variant, block_def, global_params, arch_params, num_classes,
        override_params, pretrained, progress, **kwargs):
    assert isinstance(arch_params, container_abcs.Sequence), \
        "'arch_params' should be a sequence (e.g. list or tuple)"
    
    if override_params is not None:
        assert isinstance(override_params, container_abcs.Mapping), \
            "'override_params' should be a mapping (e.g. dict)"
        global_params.update(dict(override_params))

    if not pretrained:
        kwargs['num_classes'] = num_classes

    model = EfficientNet(block_def, arch_params, global_params, **kwargs)
    if pretrained:
        state_dict = load_state_dict_from_url(model_urls[variant], progress=progress)
        model.load_state_dict(state_dict, strict=True)
        if num_classes != 1000:
            model.reset_classifier(num_classes)
    return model


def _efficientnet(variant, arch_params, num_classes=1000, override_params=None, 
                  pretrained=False, progress=True, **kwargs):
    """Creates an EfficientNet model.

    Ref impl: https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
    Paper: https://arxiv.org/abs/1905.11946

    EfficientNet params (arch_params)
    name: (channel_multiplier, depth_multiplier, resolution, dropout_rate)
    'efficientnet-b0': (1.0, 1.0, 224, 0.2),
    'efficientnet-b1': (1.0, 1.1, 240, 0.2),
    'efficientnet-b2': (1.1, 1.2, 260, 0.3),
    'efficientnet-b3': (1.2, 1.4, 300, 0.3),
    'efficientnet-b4': (1.4, 1.8, 380, 0.4),
    'efficientnet-b5': (1.6, 2.2, 456, 0.4),
    'efficientnet-b6': (1.8, 2.6, 528, 0.5),
    'efficientnet-b7': (2.0, 3.1, 600, 0.5),
    'efficientnet-b8': (2.2, 3.6, 672, 0.5),
    'efficientnet-l2': (4.3, 5.3, 800, 0.5),
    """
    block_def = [
        ['ds_r1_k3_s1_e1_c16_se0.25'],
        ['ir_r2_k3_s2_e6_c24_se0.25'],
        ['ir_r2_k5_s2_e6_c40_se0.25'],
        ['ir_r3_k3_s2_e6_c80_se0.25'],
        ['ir_r3_k5_s1_e6_c112_se0.25'],
        ['ir_r4_k5_s2_e6_c192_se0.25'],
        ['ir_r1_k3_s1_e6_c320_se0.25'],
    ]
    global_params = {
        'channel_divisor': 8,
        'channel_min': None,
        'drop_path_rate': 0.2,
        'act_layer': Swish,
        'pad_type': 'same',
        'norm_kwargs': dict(eps=TF_BN_EPSILON, momentum=TF_BN_MOMENTUM)
    }

    model = _create_model(variant, block_def, global_params, arch_params, num_classes,
        override_params, pretrained, progress, **kwargs)
    return model


def _efficientnet_edge(variant, arch_params, num_classes=1000, override_params=None, 
                  pretrained=False, progress=True, **kwargs):
    """Creates an EfficientNet-EdgeTPU model

    Ref impl: https://github.com/tensorflow/tpu/tree/master/models/official/efficientnet/edgetpu
    Blog post: https://ai.googleblog.com/2019/08/efficientnet-edgetpu-creating.html

    arch_params
    name: (channel_multiplier, depth_multiplier, resolution, dropout_rate)
    'efficientnet-edge-s': (1.0, 1.0, 224, 0.2),    # edgetpu-S
    'efficientnet-edge-m': (1.0, 1.1, 240, 0.2),    # edgetpu-m
    'efficientnet-edge-l': (1.2, 1.4, 300, 0.3),    # edgetpu-l
    """
    block_def = [
        ['er_r1_k3_s1_e4_c24_m24_noskip'],
        ['er_r2_k3_s2_e8_c32'],
        ['er_r4_k3_s2_e8_c48'],
        ['ir_r5_k5_s2_e8_c96'],
        ['ir_r4_k5_s1_e8_c144'],
        ['ir_r2_k5_s2_e8_c192'],
    ]
    global_params = {
        'channel_divisor': 8,
        'channel_min': None,
        'drop_path_rate': 0.2,
        'act_layer': nn.ReLU,
        'pad_type': 'same',
        'norm_kwargs': dict(eps=TF_BN_EPSILON, momentum=TF_BN_MOMENTUM)
    }

    model = _create_model(variant, block_def, global_params, arch_params, num_classes,
        override_params, pretrained, progress, **kwargs)
    return model


def _efficientnet_lite(variant, arch_params, num_classes=1000, override_params=None, 
                  pretrained=False, progress=True, **kwargs):
    """Creates an EfficientNet-Lite model.

    Ref impl: https://github.com/tensorflow/tpu/tree/master/models/official/efficientnet/lite
    Blog post: https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md

    arch_params
    name: (channel_multiplier, depth_multiplier, resolution, dropout_rate)
      'efficientnet-lite0': (1.0, 1.0, 224, 0.2),
      'efficientnet-lite1': (1.0, 1.1, 240, 0.2),
      'efficientnet-lite2': (1.1, 1.2, 260, 0.3),
      'efficientnet-lite3': (1.2, 1.4, 280, 0.3),
      'efficientnet-lite4': (1.4, 1.8, 300, 0.3),
    """
    block_def = [
        ['ds_r1_k3_s1_e1_c16'],
        ['ir_r2_k3_s2_e6_c24'],
        ['ir_r2_k5_s2_e6_c40'],
        ['ir_r3_k3_s2_e6_c80'],
        ['ir_r3_k5_s1_e6_c112'],
        ['ir_r4_k5_s2_e6_c192'],
        ['ir_r1_k3_s1_e6_c320'],
    ]
    global_params = {
        'channel_divisor': 8,
        'channel_min': None,
        'drop_path_rate': 0.2,
        'act_layer': nn.ReLU6,
        'pad_type': 'same',
        'norm_kwargs': dict(eps=TF_BN_EPSILON, momentum=TF_BN_MOMENTUM)
    }
    kwargs['fix_stem'] = True
    kwargs['num_features'] = 1280
    kwargs['fix_block_first_last'] = True

    model = _create_model(variant, block_def, global_params, arch_params, num_classes,
        override_params, pretrained, progress, **kwargs)
    return model


def efficientnet_b0(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B0 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b0', (1.0, 1.0, 224, 0.2),
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b1(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B1 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b1', (1.0, 1.1, 240, 0.2), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b2(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B2 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b2', (1.1, 1.2, 260, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b3(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B3 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b3', (1.2, 1.4, 300, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b4(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B4 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b4', (1.4, 1.8, 380, 0.4), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b5(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B5 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b5', (1.6, 2.2, 456, 0.4), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b6(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B6 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b6', (1.8, 2.6, 528, 0.5), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b7(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B7 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b7', (2.0, 3.1, 600, 0.5), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_b8(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-B8 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_b8', (2.2, 3.6, 672, 0.5), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_l2(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-L2 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_l2', (4.3, 5.3, 800, 0.5), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_l2_475(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-L2 with input size of 475 model from
    `"EfficientNet: Rethinking Model Scaling for CNNs" <https://arxiv.org/abs/1905.11946>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet('efficientnet_l2_475', (4.3, 5.3, 475, 0.5), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_edge_s(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-EdgeTPU-S model from
    `"EfficientNet-EdgeTPU: Creating Accelerator-Optimized Neural Networks with AutoML" 
    <https://ai.googleblog.com/2019/08/efficientnet-edgetpu-creating.html>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_edge('efficientnet_edge_s', (1.0, 1.0, 224, 0.2), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_edge_m(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-EdgeTPU-M model from
    `"EfficientNet-EdgeTPU: Creating Accelerator-Optimized Neural Networks with AutoML" 
    <https://ai.googleblog.com/2019/08/efficientnet-edgetpu-creating.html>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_edge('efficientnet_edge_m', (1.0, 1.1, 240, 0.2), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_edge_l(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-EdgeTPU-L model from
    `"EfficientNet-EdgeTPU: Creating Accelerator-Optimized Neural Networks with AutoML" 
    <https://ai.googleblog.com/2019/08/efficientnet-edgetpu-creating.html>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_edge('efficientnet_edge_l', (1.2, 1.4, 300, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def efficientnet_lite0(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-Lite0 model from
    `"Original EfficientNet-Lite Implementation in Tensorflow" 
    <https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_lite('efficientnet_lite0', (1.0, 1.0, 224, 0.2), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_lite1(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-Lite1 model from
    `"Original EfficientNet-Lite Implementation in Tensorflow" 
    <https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_lite('efficientnet_lite1', (1.0, 1.1, 240, 0.2), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_lite2(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-Lite2 model from
    `"Original EfficientNet-Lite Implementation in Tensorflow" 
    <https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_lite('efficientnet_lite2', (1.1, 1.2, 260, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_lite3(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-Lite3 model from
    `"Original EfficientNet-Lite Implementation in Tensorflow" 
    <https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_lite('efficientnet_lite3', (1.2, 1.4, 280, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model

def efficientnet_lite4(pretrained=False, progress=True, **kwargs):
    r"""EfficientNet-Lite4 model from
    `"Original EfficientNet-Lite Implementation in Tensorflow" 
    <https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/lite/README.md>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    model = _efficientnet_lite('efficientnet_lite4', (1.4, 1.8, 300, 0.3), 
        pretrained=pretrained, progress=progress, **kwargs)
    return model


def _efficientnet_stages(network: EfficientNet):
    """ get stages for Efficientnet backbone
    
    This stage division is based on EfficientDet Implementation 
    (https://github.com/google/automl/tree/master/efficientdet),
    which takes the layers with spatial reduction of 2
    """
    blocks_channels = network.out_channels[1:-2]
    channels = np.array(blocks_channels)[[0,1,2,4,-1]]
    if len(network.blocks) == 6:
        last_stage = network.blocks[5]
    elif len(network.blocks) == 7:
        last_stage = nn.Sequential(
            network.blocks[5],
            network.blocks[6]
        )
    else:
        raise RuntimeError("Unable to get stages from efficientnet network, " \
            "number of blocks in efficientnet should be 6 or 7, got %s" % len(network.blocks))
    stages = [
        nn.Sequential(
            network.conv_stem,
            network.bn1,
            network.act1,
            network.blocks[0]
        ),
        network.blocks[1],
        network.blocks[2],
        nn.Sequential(
            network.blocks[3],
            network.blocks[4]
        ),
        last_stage
    ]
    return nn.Sequential(*stages), list(channels)


def get_backbone(model_name: str, pretrained: bool = False, feature_type: str = "tri_stage_fpn", 
                 n_classes: int = 1000, **kwargs):
    if not model_name in supported_models:
        raise RuntimeError("model %s is not supported yet, available : %s" %(model_name, supported_models))

    kwargs['override_params'] = {
        'drop_path_rate': 0.0
    }
    network = eval('{}(pretrained=pretrained, num_classes=n_classes, **kwargs)'.format(model_name))
    stages, channels = _efficientnet_stages(network)

    if feature_type == "tri_stage_fpn":
        backbone = Backbone(stages, channels)
    elif feature_type == "classifier":
        backbone = ClassifierFeature(stages, network.get_classifier(), n_classes)
    else:
        raise NotImplementedError("'feature_type' for other than 'tri_stage_fpn' and 'classifier'"\
            "is not currently implemented, got %s" % (feature_type))
    return backbone
