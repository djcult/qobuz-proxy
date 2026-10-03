"""Configuration and factory wiring for the ALSA backend."""

from unittest.mock import patch

import pytest

from qobuz_proxy.backends.alsa import AlsaAudioBackend
from qobuz_proxy.backends.factory import BackendFactory
from qobuz_proxy.config import Config, ConfigError, dict_to_config, validate_config


def test_alsa_config_parses_exact_device_and_latency() -> None:
    config = dict_to_config(
        {"backend": {"type": "alsa", "alsa": {"device": "hw:Audiolab,0", "latency_us": 250000}}}
    )
    assert config.backend.alsa.device == "hw:Audiolab,0"
    assert config.backend.alsa.latency_us == 250000


@pytest.mark.parametrize("device", ["default", "plughw:1,0", "sysdefault"])
def test_alsa_config_rejects_conversion_devices(device: str) -> None:
    config = Config()
    config.backend.type = "alsa"
    config.backend.alsa.device = device
    with pytest.raises(ConfigError, match="explicit hw:"):
        validate_config(config)


async def test_factory_creates_configured_alsa_backend() -> None:
    config = Config()
    config.backend.type = "alsa"
    config.backend.alsa.device = "hw:Audiolab,0"
    with patch.object(AlsaAudioBackend, "connect", return_value=True):
        backend = await BackendFactory.create_from_config(config)
    assert isinstance(backend, AlsaAudioBackend)
    assert backend.get_info().device_id == "alsa-hw:Audiolab,0"
