from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Config:
    demo_mode: bool = os.getenv('BULBAX_DEMO_MODE', '1') != '0'
    mainnet_enabled: bool = False
    ai_can_sign: bool = False
    require_explicit_confirmation: bool = True

CONFIG = Config()
