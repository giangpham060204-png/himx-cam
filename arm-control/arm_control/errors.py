"""Expected arm-control failures."""


class ArmError(Exception):
    """Base class for expected arm-control failures."""


class ConfigError(ArmError):
    """The TOML config is missing or invalid."""


class UnreachableError(ArmError):
    """The requested tip pose has no joint solution inside the limits."""


class NotHomedError(ArmError):
    """Motion was requested before the axis was homed."""


class OutOfRangeError(ArmError):
    """A joint target is outside the configured soft limits."""


class MoonrakerError(ArmError):
    """Moonraker returned an error or an unexpected response."""


class MoonrakerUnavailable(MoonrakerError):
    """Moonraker could not be reached."""


class MoonrakerTimeout(MoonrakerError):
    """Moonraker did not answer within the timeout."""


class TmcFaultError(ArmError):
    """A TMC2209 driver reported a fault."""
