"""
Gateway and Battery data models.

GatewayProfile  — flat hardware profile discovered once at startup.
GatewayRecord   — what is stored in the DB and returned by the API.
BatteryUnit     — per-aPower battery unit.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BatteryUnit:
    """Represents one aPower battery unit linked to a parent aGate."""
    short_id: str           # Last 8 chars of aPower serial
    full_serial: str        # Full aPower serial (for Cloud API)
    agate_short_id: str     # Parent aGate short_id
    slot_index: int = 1     # 1-indexed position in aGate
    rated_kw: float = 0.0  # Rated continuous power (kW)
    rated_kwh: float = 0.0 # Rated energy capacity (kWh)


@dataclass
class GatewayProfile:
    """
    Flat hardware profile for one aGate — discovered once at startup.
    Drives entity suppression: entities are only published if the
    corresponding hardware flag is True.
    """
    # Identity
    short_id: str           # Last 8 chars of aGate serial, e.g. "99900001"
    full_serial: str        # Full serial, e.g. "99900000000099900001"
    name: str = ""          # User-assigned label

    # Cloud API identifiers (internal use only)
    site_id: str = ""

    # Hardware info (from Cloud API discovery)
    model: str = ""         # e.g. "aGate X-01-AU"
    hw_version: int = 0

    # Connected batteries
    batteries: list[BatteryUnit] = field(default_factory=list)

    # Hardware feature flags (drive entity suppression)
    has_solar: bool = False
    has_generator: bool = False
    has_smart_circuits: bool = False
    smart_circuit_count: int = 0
    sw_merge: bool = False          # US V2: circuits 1+2 physically merged (SwMerge flag)
    has_v2l: bool = False
    has_apbox: bool = False
    has_ahub: bool = False          # aHub accessory connected (aPower S companion)
    three_phase: bool = False
    has_mppt: bool = False          # DC-coupled MPPT (aPower S built-in)
    mppt_enabled: bool = False      # MPPT flag from Cloud discover
    remote_solar: bool = False      # Remote PV detected via aHub/aPBox
    has_split_ct: bool = False      # Split-CT PV meter installed (ct_split_pv flag)

    @property
    def battery_count(self) -> int:
        return len(self.batteries)

    @property
    def has_multi_battery(self) -> bool:
        return self.battery_count > 1

    @property
    def total_rated_kwh(self) -> float:
        return sum(b.rated_kwh for b in self.batteries)

    def to_dict(self) -> dict:
        return {
            "short_id": self.short_id,
            "full_serial": self.full_serial,
            "name": self.name,
            "site_id": self.site_id,
            "model": self.model,
            "hw_version": self.hw_version,
            "battery_count": self.battery_count,
            "total_rated_kwh": self.total_rated_kwh,
            "has_solar": self.has_solar,
            "has_generator": self.has_generator,
            "has_smart_circuits": self.has_smart_circuits,
            "smart_circuit_count": self.smart_circuit_count,
            "sw_merge": self.sw_merge,
            "has_v2l": self.has_v2l,
            "has_apbox": self.has_apbox,
            "has_ahub": self.has_ahub,
            "three_phase": self.three_phase,
            "has_mppt": self.has_mppt,
            "mppt_enabled": self.mppt_enabled,
            "remote_solar": self.remote_solar,
            "has_split_ct": self.has_split_ct,
        }


@dataclass
class GatewayRecord:
    """
    Gateway as stored in DB and returned by the API.
    Combines GatewayProfile with runtime status.
    """
    short_id: str
    full_serial: str
    name: str
    model: str
    enabled: bool
    last_seen: Optional[str]
    batteries: list[BatteryUnit] = field(default_factory=list)

    # Runtime status (not persisted — updated by GatewayService)
    poll_status: str = "unknown"    # ok | error | polling | stopped
    last_poll_age_s: Optional[int] = None
    mqtt_published: bool = False

    def to_api_dict(self) -> dict:
        return {
            "short_id": self.short_id,
            "full_serial": self.full_serial,
            "name": self.name or self.short_id,
            "model": self.model,
            "enabled": self.enabled,
            "last_seen": self.last_seen,
            "battery_count": len(self.batteries),
            "poll_status": self.poll_status,
            "last_poll_age_s": self.last_poll_age_s,
            "mqtt_published": self.mqtt_published,
        }


def make_short_id(serial: str) -> str:
    """Return the last 8 characters of a hardware serial as the short identifier in uppercase."""
    return (serial[-8:] if len(serial) >= 8 else serial).upper()


#: Mock and demo gateways start here, in the serial and in the short id.
#: `999` is not a prefix FranklinWH issues, so a mock is recognisable on sight
#: in an entity id, an MQTT topic, a log line or a support screenshot — which
#: matters because demo data that looks real is demo data that gets acted on.
MOCK_SERIAL_PREFIX = "999"

#: Real serials are 20 characters.
SERIAL_LENGTH = 20


#: A fictional serial shaped like a real one — 20 characters with an alpha run
#: in the middle — for tests and documentation that need to exercise *case*.
#: `make_mock_serial()` is all digits by design, so it cannot stand in for a
#: serial whose letters are the point: upper vs lower casing, sanitisation, and
#: the duplicate-device bug that a lower-cased identifier caused.
#:
#: Not issued by FranklinWH, and not anybody's hardware.
FIXTURE_SERIAL = "1006000AB0CD00000042"


def make_mock_serial(index: int = 1) -> str:
    """A serial for a mock or demo gateway. Numeric throughout.

    Two properties, both deliberate:

    * **All digits.** The short id becomes part of MQTT topics and entity ids,
      and Home Assistant forces entity ids to lowercase. A serial containing
      letters therefore appears in two cases — `A02F` and `a02f` — which is
      hard to read and has already produced a duplicate device here. A numeric
      serial cannot have that problem.
    * **Starts 999, and so does the short id.** Visible at both lengths,
      wherever it surfaces.

    >>> make_mock_serial(1)
    '99900000000099900001'
    >>> make_short_id(make_mock_serial(1))
    '99900001'
    """
    if not 0 <= index <= 99999:
        raise ValueError(f"mock index {index} out of range (0-99999)")
    short = f"{MOCK_SERIAL_PREFIX}{index:05d}"          # 8 digits
    padding = SERIAL_LENGTH - len(MOCK_SERIAL_PREFIX) - len(short)
    return f"{MOCK_SERIAL_PREFIX}{'0' * padding}{short}"


def is_mock_serial(serial: str) -> bool:
    """Whether this serial belongs to a mock or demo gateway.

    Checks the short id as well as the serial, because the short id is what
    appears in topics and entity ids and is often all that is to hand.
    """
    serial = (serial or "").strip()
    if not serial:
        return False
    return serial.startswith(MOCK_SERIAL_PREFIX) or \
        make_short_id(serial).startswith(MOCK_SERIAL_PREFIX)
