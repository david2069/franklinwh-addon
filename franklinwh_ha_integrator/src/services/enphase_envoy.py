import requests
import json
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
from urllib.parse import urljoin
import logging
import urllib3

logger = logging.getLogger(__name__)

class EnvoyRegion(Enum):
    UNKNOWN = "unknown"
    NORTH_AMERICA = "na"
    EUROPE = "eu"
    AUSTRALIA = "au"
    UNITED_KINGDOM = "uk"
    NEW_ZEALAND = "nz"
    OTHER_INTERNATIONAL = "intl"

class DPELCapability(Enum):
    UNKNOWN = "unknown"
    NOT_SUPPORTED = "not_supported"
    SUPPORTED = "supported"
    REQUIRES_FIRMWARE = "requires_firmware_update"

@dataclass
class EnvoyCapabilities:
    serial_number: str
    firmware_version: str
    region: EnvoyRegion
    region_confidence: str
    dpel_capable: DPELCapability
    dpel_endpoints: List[str]
    dpel_reason: str
    has_export_limiting: bool
    has_frequency_watt: bool
    has_volt_watt: bool
    has_active_power_control: bool
    grid_standard: Optional[str]
    max_export_limit_watts: Optional[int]
    raw_info: Dict[str, Any]
    # Which detection methods actually answered. Every probe is wrapped in a
    # bare `except: continue`, so a detector that reached nothing at all still
    # returned a full capabilities object with "unknown" in every field — and
    # the UI reported "Envoy connected" on the strength of it.
    probes_succeeded: List[str] = None

    @property
    def reachable(self) -> bool:
        return bool(self.probes_succeeded)

class EnvoyCapabilityDetector:
    NON_DPEL_SERIES = ["R", "P", "C"]
    
    def __init__(self, host: str, use_https: bool = True, token: Optional[str] = None):
        self.host = host
        self.base_url = f"{'https' if use_https else 'http'}://{host}"
        self.session = requests.Session()
        self.session.verify = False
        urllib3.disable_warnings()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
    
    def detect_capabilities(self) -> EnvoyCapabilities:
        info = {
            "serial_number": "unknown",
            "firmware_version": "unknown",
            "region": EnvoyRegion.UNKNOWN,
            "region_confidence": "low",
        }
        
        detection_methods = [
            self._detect_from_info_endpoint,
            self._detect_from_home_endpoint,
            self._detect_from_production_endpoint,
            self._detect_from_grid_profile,
        ]
        
        succeeded: List[str] = []
        for method in detection_methods:
            try:
                result = method()
                info.update(result)
                succeeded.append(method.__name__)
                if info.get("region") != EnvoyRegion.UNKNOWN:
                    break
            except Exception as e:
                logger.debug(f"Envoy detect method failed: {method.__name__}: {e}")
                continue

        if not succeeded:
            logger.warning(
                f"Envoy at {self.base_url}: every detection method failed — "
                "nothing was read from the device"
            )
        
        dpel_info = self._assess_dpel_capability(info)
        
        dpel_endpoints = []
        if dpel_info["capable"] in [DPELCapability.SUPPORTED, DPELCapability.UNKNOWN]:
            dpel_endpoints = self._probe_dpel_endpoints()
            if dpel_endpoints:
                dpel_info["capable"] = DPELCapability.SUPPORTED
        
        return EnvoyCapabilities(
            serial_number=info.get("serial_number", "unknown"),
            firmware_version=info.get("firmware_version", "unknown"),
            region=info.get("region", EnvoyRegion.UNKNOWN),
            region_confidence=info.get("region_confidence", "low"),
            dpel_capable=dpel_info["capable"],
            dpel_endpoints=dpel_endpoints,
            dpel_reason=dpel_info["reason"],
            has_export_limiting=dpel_info.get("has_export_limiting", False),
            has_frequency_watt=info.get("has_frequency_watt", False),
            has_volt_watt=info.get("has_volt_watt", False),
            has_active_power_control=dpel_info.get("has_active_power_control", False),
            grid_standard=info.get("grid_standard"),
            max_export_limit_watts=info.get("max_export_limit_watts"),
            raw_info=info,
            probes_succeeded=succeeded,
        )
    
    def _detect_from_info_endpoint(self) -> Dict[str, Any]:
        url = urljoin(self.base_url, "/info")
        resp = self.session.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        result = {
            "serial_number": data.get("serial", {}).get("num", "unknown"),
            "firmware_version": data.get("software", "unknown"),
        }
        fw = result["firmware_version"]
        if fw != "unknown":
            result.update(self._parse_firmware_region(fw))
        if "grid_profile" in data:
            gp = data["grid_profile"]
            result["grid_standard"] = gp.get("name") or gp.get("standard")
            result["has_export_limiting"] = gp.get("export_limiting", False)
        return result
    
    def _detect_from_home_endpoint(self) -> Dict[str, Any]:
        url = urljoin(self.base_url, "/home")
        resp = self.session.get(url, timeout=5)
        version_match = re.search(r'Envoy\s+([A-Z]?\d+\.\d+\.\d+)', resp.text)
        if version_match:
            return self._parse_firmware_region(version_match.group(1))
        text_lower = resp.text.lower()
        if "as4777" in text_lower or "australia" in text_lower:
            return {"region": EnvoyRegion.AUSTRALIA, "region_confidence": "medium"}
        elif "en50549" in text_lower or "europe" in text_lower:
            return {"region": EnvoyRegion.EUROPE, "region_confidence": "medium"}
        return {}
    
    def _detect_from_production_endpoint(self) -> Dict[str, Any]:
        url = urljoin(self.base_url, "/api/v1/production")
        resp = self.session.get(url, timeout=5)
        if resp.status_code == 200:
            return {"has_active_power_control": "activePowerControl" in resp.text}
        return {}
    
    def _detect_from_grid_profile(self) -> Dict[str, Any]:
        for ep in ["/admin/lib/grid_profile", "/ivp/grid_profile", "/api/v1/grid_profile"]:
            try:
                url = urljoin(self.base_url, ep)
                resp = self.session.get(url, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    standard = data.get("standard") or data.get("name", "")
                    standard_lower = standard.lower()
                    result = {
                        "grid_standard": standard,
                        "has_export_limiting": data.get("export_limiting", False),
                        "max_export_limit_watts": data.get("max_export_watts"),
                    }
                    if any(x in standard_lower for x in ["as4777", "australia", "au"]):
                        result["region"] = EnvoyRegion.AUSTRALIA
                        result["region_confidence"] = "high"
                    elif any(x in standard_lower for x in ["en50549", "g99", "g100", "uk"]):
                        result["region"] = EnvoyRegion.UNITED_KINGDOM
                        result["region_confidence"] = "high"
                    elif any(x in standard_lower for x in ["vde", "germany", "eu"]):
                        result["region"] = EnvoyRegion.EUROPE
                        result["region_confidence"] = "high"
                    elif "ieee" in standard_lower or "csip" in standard_lower:
                        result["region"] = EnvoyRegion.NORTH_AMERICA
                        result["region_confidence"] = "high"
                    return result
            except:
                continue
        return {}
    
    def _parse_firmware_region(self, firmware: str) -> Dict[str, Any]:
        fw_upper = firmware.upper()
        if fw_upper.startswith("D"):
            return {"firmware_version": firmware, "region": EnvoyRegion.OTHER_INTERNATIONAL, "region_confidence": "medium", "firmware_series": "D", "likely_dpel_capable": True}
        if fw_upper.startswith("R"): return {"firmware_version": firmware, "region": EnvoyRegion.NORTH_AMERICA, "region_confidence": "high", "firmware_series": "R", "likely_dpel_capable": False}
        if fw_upper.startswith("P"): return {"firmware_version": firmware, "region": EnvoyRegion.NORTH_AMERICA, "region_confidence": "high", "firmware_series": "P", "likely_dpel_capable": False}
        if fw_upper.startswith("C"): return {"firmware_version": firmware, "region": EnvoyRegion.NORTH_AMERICA, "region_confidence": "high", "firmware_series": "C", "likely_dpel_capable": False}
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', firmware)
        if match and int(match.group(1)) >= 7:
            return {"firmware_version": firmware, "region": EnvoyRegion.UNKNOWN, "region_confidence": "low", "likely_dpel_capable": "unknown"}
        return {"firmware_version": firmware}
    
    def _assess_dpel_capability(self, info: Dict) -> Dict[str, Any]:
        region = info.get("region", EnvoyRegion.UNKNOWN)
        fw = info.get("firmware_version", "unknown")
        fw_series = info.get("firmware_series", "")
        if region == EnvoyRegion.NORTH_AMERICA or fw_series in self.NON_DPEL_SERIES:
            return {"capable": DPELCapability.NOT_SUPPORTED, "reason": "Model/Region does not support DPEL"}
        if info.get("has_export_limiting") is True or fw_series == "D" or fw.upper().startswith("D"):
            return {"capable": DPELCapability.SUPPORTED, "reason": "Supported via firmware or grid profile"}
        return {"capable": DPELCapability.UNKNOWN, "reason": "Requires endpoint probe"}
    
    def _probe_dpel_endpoints(self) -> List[str]:
        found = []
        for ep in ["/admin/lib/dpel", "/ivp/peb/dpel", "/ivp/ss/dpel", "/admin/dpel", "/api/v1/dpel", "/installer/dpel"]:
            try:
                resp = self.session.get(urljoin(self.base_url, ep), timeout=3)
                if resp.status_code in [200, 401]:
                    found.append(ep)
            except: pass
        return found

class DPELController:
    def __init__(self, host: str, username: str, password: str, token: Optional[str] = None):
        self.host = host
        self.username = username
        self.password = password
        self.token = token
        self.detector = EnvoyCapabilityDetector(host, token=token)
        self.capabilities: Optional[EnvoyCapabilities] = None
        self._auth_session = requests.Session()
        self._auth_session.verify = False
        urllib3.disable_warnings()
        if token:
            self._auth_session.headers["Authorization"] = f"Bearer {token}"
    
    def initialize(self) -> EnvoyCapabilities:
        self.capabilities = self.detector.detect_capabilities()
        return self.capabilities
    
    def _auth_request(self, method, url, **kwargs) -> requests.Response:
        if self.token:
            return self._auth_session.request(method, url, **kwargs)
        # Assuming D7+ firmware typically uses bearer token or digest. We'll try basic/digest for now.
        from requests.auth import HTTPDigestAuth
        return self._auth_session.request(method, url, auth=HTTPDigestAuth(self.username, self.password), **kwargs)
        
    def set_export_limit(self, watt: int = 0, slew_rate: int = 500, force: bool = False) -> Dict[str, Any]:
        if not self.capabilities: self.initialize()
        if self.capabilities.dpel_capable == DPELCapability.NOT_SUPPORTED and not force:
            return {"success": False, "error": "DPEL not supported on this Envoy"}
        
        if self.capabilities.dpel_endpoints:
            return self._try_dpel_control(watt, slew_rate)
        if self.capabilities.has_active_power_control:
            return self._try_active_power_control(watt, slew_rate)
            
        return {"success": False, "error": "No viable DPEL control method detected"}
        
    def _try_dpel_control(self, watt: int, slew_rate: int) -> Dict[str, Any]:
        payload = {"pel_limit": watt, "slew_rate": slew_rate}
        for ep in self.capabilities.dpel_endpoints:
            try:
                url = urljoin(f"https://{self.host}", ep)
                resp = self._auth_request('PUT', url, json=payload, timeout=10)
                if resp.status_code in [200, 204]:
                    return {"success": True, "method": "dpel", "endpoint": ep}
                resp = self._auth_request('POST', url, json=payload, timeout=10)
                if resp.status_code in [200, 204]:
                    return {"success": True, "method": "dpel", "endpoint": ep}
            except Exception as e:
                logger.error(f"DPEL control failed on {ep}: {e}")
        return {"success": False, "error": "All DPEL endpoints failed"}

    def _try_active_power_control(self, watt: int, slew_rate: int) -> Dict[str, Any]:
        payload = {"active_power_limit": watt, "ramp_rate": slew_rate}
        for ep in ["/ivp/apc", "/admin/lib/apc", "/api/v1/apc"]:
            try:
                url = urljoin(f"https://{self.host}", ep)
                resp = self._auth_request('PUT', url, json=payload, timeout=10)
                if resp.status_code in [200, 204]:
                    return {"success": True, "method": "apc", "endpoint": ep}
            except: pass
        return {"success": False, "error": "All APC endpoints failed"}

