from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, Optional

import torch
from diffusers import UNet2DConditionModel

from invokeai.backend.stable_diffusion.extensions.base import ExtensionBase

if TYPE_CHECKING:
    from invokeai.app.shared.models import FreeUConfig


class FreeUExt(ExtensionBase):
    def __init__(
        self,
        freeu_config: FreeUConfig,
    ):
        super().__init__()
        self._freeu_config = freeu_config

    @contextmanager
    def patch_unet(self, unet: UNet2DConditionModel, cached_weights: Optional[Dict[str, torch.Tensor]] = None):
        unet.enable_freeu(
            b1=self._freeu_config.b1,
            b2=self._freeu_config.b2,
            s1=self._freeu_config.s1,
            s2=self._freeu_config.s2,
        )

        try:
            yield
        finally:
            unet.disable_freeu()
