import json
import logging
import dataclasses
from typing import Dict, Any

from franklinwh_cloud import FranklinWHCloud

from src.services import db
from src.services.gateway_registry import GatewayRegistry

logger = logging.getLogger(__name__)




def build_profile_from_snapshot(snapshot: dict, gw: dict | None = None,
                                short_id: str = "") -> dict:
    """Build the Tier-A static profile from a discover(tier=3) snapshot.

    Extracted so onboarding and the on-demand refresh share ONE definition. A
    second copy of this mapping would drift from the first within a release,
    which is how every duplicated-truth bug in this repo began.

    Discover is install-centric: these fields describe what a technician
    physically fitted, so they are re-read when the install changes, not on a
    timer. Two exceptions drift on their own and are worth knowing about —
    firmware moves with OTA updates, and pto_date is set when the utility grants
    permission. Neither involves anyone visiting the site.
    """
    gw = gw or {}
    agate_info  = snapshot.get("agate", {}) or {}
    site_snap   = snapshot.get("site", {}) or {}
    flags       = snapshot.get("flags", {}) or {}
    accessories = snapshot.get("accessories", {}) or {}
    batteries_s = snapshot.get("batteries", {}) or {}
    warranty    = snapshot.get("warranty", {}) or {}
    network     = snapshot.get("network", {}) or {}
    sc_info     = accessories.get("smart_circuits", {}) or {}

    site_id   = str(site_snap.get("site_id", ""))
    site_name = site_snap.get("site_name", "") or "Home"

    device_name = (gw.get("name") or "").strip()
    if not device_name or device_name.lower() in ("agate", "agate x"):
        device_name = (agate_info.get("name") or site_snap.get("gateway_name") or "").strip()
    if not device_name:
        device_name = short_id

    profile = {
        # ── Capability flags (from discover flags) ──────────────────────
        "has_solar":          flags.get("solar", False),
        "has_smart_circuits": accessories.get("has_smart_circuits", False),
        "has_generator":      accessories.get("has_generator", False),
        "has_apbox":          accessories.get("has_apbox", False),
        "has_ahub":           accessories.get("has_ahub", False),
        "has_mac1":           accessories.get("has_mac1", False),
        # Not in discover(); patched in by refresh_gateway_profile from
        # get_span_setting(). Records that the SPAN integration was CONFIGURED,
        # not that a panel was detected — 0 does not mean no panel is present.
        # None means "not yet determined".
        "span_configured":    accessories.get("span_configured"),
        "three_phase":        flags.get("three_phase", False),
        "mppt_enabled":       flags.get("mppt_enabled", False),
        "ct_split_grid":      flags.get("ct_split_grid", False),
        "ct_split_pv":        flags.get("ct_split_pv", False),
        "remote_solar":       flags.get("remote_solar", False),
        "v2l_eligible":       flags.get("v2l_eligible", False),

        # ── aGate hardware identity ─────────────────────────────────────
        "sku":            agate_info.get("sku", ""),
        "model_name":     agate_info.get("model_name", ""),
        "model":          agate_info.get("model", ""),
        "hw_version":     agate_info.get("hw_version"),      # int e.g. 102
        "hw_version_str": agate_info.get("hw_version_str", ""),
        "generation":     agate_info.get("generation"),       # 1 or 2
        "protocol_ver":   agate_info.get("protocol_ver", ""),

        # ── Firmware versions (set at install, updated only by OTA) ─────
        "firmware":       agate_info.get("firmware", ""),
        "ibg_version":    agate_info.get("ibg_version", ""),
        "sl_version":     agate_info.get("sl_version", ""),
        "aws_version":    agate_info.get("aws_version", ""),
        "app_version":    agate_info.get("app_version", ""),
        "meter_version":  agate_info.get("meter_version", ""),

        # ── Connectivity type (WiFi=3, 4G=4, Eth=1 — set at install) ───
        "conn_type":      gw.get("connType") or agate_info.get("conn_type"),
        "conn_type_name": agate_info.get("conn_type_name", ""),

        # ── Site / location identity ────────────────────────────────────
        "site_name":    site_name,
        "site_id":      site_id,
        "site_address": site_snap.get("address", ""),
        "country_id":   gw.get("countryId") or site_snap.get("country_id"),
        "province_id":  gw.get("provinceId") or site_snap.get("province_id"),
        "timezone":     gw.get("zoneInfo") or site_snap.get("timezone", ""),
        "country_code": site_snap.get("alpha_code", ""),
        "pto_date":     site_snap.get("pto_date", ""),

        # ── Smart Circuits (count/model set at install) ─────────────────
        "smart_circuit_count":   sc_info.get("count", 0) if sc_info else 0,
        "sc_version":            sc_info.get("version")  if sc_info else None,
        "sc_v2l_port":           sc_info.get("v2l_port", False) if sc_info else False,

        # ── Battery inventory (static — count/capacity set at install) ──
        "apower_count":      batteries_s.get("count", 0),
        "total_kwh":         batteries_s.get("total_capacity_kwh"),
        "total_rated_kw":    batteries_s.get("total_rated_power_kw"),
        "apower_units": [
            {
                "serial":         u.get("serial", ""),
                "rated_kw":       u.get("rated_power_kw"),
                "rated_kwh":      u.get("rated_capacity_kwh"),
                "pe_hw_ver":      u.get("pe_hw_ver", ""),
                "fpga_ver":       u.get("fpga_ver", ""),
                "dcdc_ver":       u.get("dcdc_ver", ""),
                "inv_ver":        u.get("inv_ver", ""),
                "bms_ver":        u.get("bms_ver", ""),
            }
            for u in (batteries_s.get("units") or [])
        ],

        # ── Grid static parameters (set by installer) ───────────────────
        "feed_max_kw":   snapshot.get("grid", {}).get("feed_max_kw"),
        "import_max_kw": snapshot.get("grid", {}).get("import_max_kw"),

        # ── Warranty (static — set at activation) ─────────────────────
        "warranty_expiry":      warranty.get("expiry", ""),
        "installer_company":    warranty.get("installer_company", ""),
        "installer_phone":      warranty.get("installer_phone", ""),

        # ── Activation dates ───────────────────────────────────────────
        "activated_at": agate_info.get("activated", ""),
        "created_at_cloud": agate_info.get("created", ""),

        # Resolved above from the cloud name, the snapshot, then the short_id.
        # Set inside the builder rather than patched on afterwards: the refresh
        # path does not run onboarding's later assignments, so a field set
        # outside the builder came back null on the first live run.
        "gateway_name": device_name,

        # ── Marker: schema version for future migrations ───────────────
        "_profile_version": 2,
        "_discovered_at":   __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

    return profile

class IntegrationManager:
    """
    Global Orchestrator mapping the 'Zero-Touch Onboarding Pipeline'.
    Responsible for establishing a single global Cloud API session, mapping out
    all tied Sites and Gateways, and securely persisting them to the SQLite holding bay
    without actively spinning up telemetry daemons.
    """

    def __init__(self, registry: GatewayRegistry):
        self.registry = registry

    async def discover_and_onboard(self, email: str, password: str) -> Dict[str, Any]:
        """
        Authenticate globally, discover all available sites/gateways, and 
        populate the database holding bay. Gateways natively default to disabled (opt-in).
        """
        logger.info(f"IntegrationManager: Initiating global discovery sweep for {email}")
        
        try:
            # 1. Authenticate via FranklinWHCloud facade (v0.4.5+)
            #    select_gateway() without args auto-discovers via get_home_gateway_list() proxy
            fwh = FranklinWHCloud(email=email, password=password)
            await fwh.login()
            await fwh.select_gateway()

            # 2. Fetch lightweight gateway inventory
            #    get_home_gateway_list() is the canonical lightweight Account Phase endpoint
            #    designed specifically for hardware discovery (stable, no schema drift risk)
            gw_res   = await fwh.get_home_gateway_list()
            gateways = gw_res.get("result", []) if isinstance(gw_res, dict) else []

            # Phase 112: Auto-Registry Logic
            total_agates  = len(gateways)
            auto_register = (total_agates == 1)

            logger.info(
                f"IntegrationManager: Gateway inventory found {total_agates} aGates. "
                f"Auto-Register: {auto_register}"
            )

            discovered_count = 0

            for gw in gateways:
                # Serial is in field "id" from get_home_gateway_list() response
                full_serial = gw.get("id", "")
                if not full_serial:
                    continue

                short_id = full_serial[-8:]

                # 3. Rebind facade to this specific serial, run full Tier-3 discovery.
                #    Tier 3 internally calls get_site_and_device_info() to populate
                #    snap.site.site_id and snap.site.site_name — no separate outer call needed.
                await fwh.select_gateway(full_serial)
                raw_snapshot = await fwh.discover(tier=3)
                snapshot    = dataclasses.asdict(raw_snapshot) if dataclasses.is_dataclass(raw_snapshot) else raw_snapshot

                # 4. Extract physical traits and site identity from snapshot
                agate_info  = snapshot.get("agate", {})
                site_snap   = snapshot.get("site", {})
                flags       = snapshot.get("flags", {})
                accessories = snapshot.get("accessories", {})
                batteries_s = snapshot.get("batteries", {})
                warranty    = snapshot.get("warranty", {})
                network     = snapshot.get("network", {})
                sc_info     = accessories.get("smart_circuits", {})

                site_id     = str(site_snap.get("site_id", ""))
                site_name   = site_snap.get("site_name", "") or "Home"

                # Gateway name: prefer user-assigned cloud name from gw list ("name" field),
                # fall back to discover snapshot, then short_id.
                # get_home_gateway_list() "name" is the user-assigned cloud name (e.g. "FHP").
                device_name = (gw.get("name") or "").strip()
                if not device_name or device_name.lower() in ("agate", "agate x"):
                    device_name = (agate_info.get("name") or site_snap.get("gateway_name") or "").strip()
                if not device_name:
                    device_name = short_id

                logger.info(f"IntegrationManager: Discovered aGate {full_serial} ('{device_name}') at Site '{site_name}'")

                # 5. Build FULL Tier-A static profile payload.
                #    Rule: every field here is read ONCE at discovery/onboarding and persisted
                #    to profile_json. The poll loop reads from profile_json — never from the cloud.
                #
                #    Field taxonomy from DISCOVER_ARCHITECTURE.md:
                #      Tier A = hardware constants that never change without a physical install change.
                #      These must NEVER be re-read on every restart.
                profile = build_profile_from_snapshot(snapshot, gw, short_id)

                # ── Grid profile name & Electricity Type (Tier A — set by installer, read once) ───────
                # Fetched separately since it's not in discover(tier=3).
                electricity_type = None
                try:
                    _gp_res    = await fwh.get_grid_profile_info(requestType=1)
                    _gp_list   = _gp_res.get("list", []) if isinstance(_gp_res, dict) else []
                    _cur_id    = _gp_res.get("currentId")
                    _gp_match  = next((p for p in _gp_list if p.get("id") == _cur_id), None)
                    _gp_name   = _gp_match.get("name", "") if _gp_match else ""
                    if _gp_name:
                        profile["grid_profile_name"] = _gp_name
                        profile["grid_profile_id"]   = _cur_id
                        electricity_type = _gp_res.get("electricityType")
                        logger.info(f"IntegrationManager: Grid profile: '{_gp_name}' (id={_cur_id}), type={electricity_type}")
                except Exception as _gpe:
                    logger.debug(f"IntegrationManager: Grid profile fetch skipped: {_gpe}")

                # ── Power Control Settings (Tier A — read once) ────────────────────
                not_control_export_solar = None
                grid_feed_max = None
                grid_max = None
                try:
                    _ps = await fwh.get_power_control_settings()
                    _p = _ps.get("result", _ps) if isinstance(_ps, dict) else _ps
                    if isinstance(_p, dict):
                        not_control_export_solar = 1 if _p.get("notControlExportSolar") else 0 if _p.get("notControlExportSolar") is not None else None
                        grid_feed_max = _p.get("globalGridDischargeMax")
                        grid_max = _p.get("globalGridChargeMax")
                        logger.info(f"IntegrationManager: Power settings: notControlExportSolar={not_control_export_solar}, grid_feed={grid_feed_max}")
                except Exception as _pse:
                    logger.debug(f"IntegrationManager: Power settings fetch skipped: {_pse}")

                # ── Gateway user-assigned cloud name (Tier A — read once) ───────────
                # get_home_gateway_list() already has it as gw["name"]. As a belt-and-suspenders
                # check, also try get_device_detail() which exposes "gatewayName" directly.
                if not device_name or device_name == short_id:
                    try:
                        _det     = await fwh.get_device_detail()
                        _det_res = _det.get("result", {}) if isinstance(_det, dict) else {}
                        _det_name = (_det_res.get("gatewayName") or "").strip()
                        if _det_name:
                            device_name = _det_name
                            logger.info(f"IntegrationManager: Gateway name from device_detail: '{device_name}'")
                    except Exception as _ne:
                        logger.debug(f"IntegrationManager: device_detail name fetch skipped: {_ne}")

                # 6. Commit to the database holding bay
                existing      = await db.get_gateway(short_id)
                enabled_state = existing.get("enabled", 0) if existing else (1 if auto_register else 0)

                await db.upsert_gateway(
                    short_id=short_id,
                    full_serial=full_serial,
                    name=device_name,
                    site_id=site_id,
                    model=agate_info.get("model", agate_info.get("model_name", "")),
                    profile=profile,
                    enabled=bool(enabled_state),
                    electricity_type=electricity_type,
                    grid_feed_max=grid_feed_max,
                    grid_max=grid_max,
                    not_control_export_solar=not_control_export_solar
                )

                # 6b. Persist battery inventory to batteries table (one row per aPower unit)
                for _idx, _unit in enumerate(batteries_s.get("units") or [], start=1):
                    _bsn = _unit.get("serial", "")
                    if _bsn:
                        await db.upsert_battery(
                            short_id=_bsn[-8:],
                            full_serial=_bsn,
                            agate_short_id=short_id,
                            rated_kw=_unit.get("rated_power_kw") or 0.0,
                            rated_kwh=_unit.get("rated_capacity_kwh") or 0.0,
                            slot_index=_idx,
                        )

                # Also persist credentials for this serial to satisfy GatewayService auth mapping
                await db.upsert_credentials(
                    full_serial=full_serial,
                    email=email,
                    password=password,
                    source="IntegrationManager"
                )

                discovered_count += 1

                # 7. If the service is already running, push the freshly discovered profile into its
                #    context and invalidate its discovery cache so the next poll republishes with
                #    the correct model, firmware, sku, and site fields.
                svc = self.registry.get_gateway(short_id)
                if svc:
                    svc.context["profile"] = profile
                    svc.context["name"]    = device_name
                    svc.status.mqtt_published = False
                    logger.info(f"IntegrationManager: Refreshed context + reset mqtt_published for {short_id}.")

                # 8. Auto-boot daemon if single-gateway zero-touch setup AND not already running
                if auto_register and not existing:
                    logger.info(f"IntegrationManager: Auto-registering and booting GatewayService for {short_id}...")
                    await self.registry.start_gateway(short_id)

            return {
                "ok": True,
                "message": f"Global discovery complete. {discovered_count} aGates mapped.",
                "discovered": discovered_count,
                "auto_registered": auto_register,
            }

        except Exception as e:
            logger.exception("IntegrationManager: Fatal error during global discovery sweep.")
            return {
                "ok": False,
                "error": str(e)
            }


async def refresh_gateway_profile(short_id: str) -> dict:
    """Re-run discovery for one gateway and rewrite its stored profile.

    Discover is install-centric — it describes what is physically fitted — so
    this is deliberately on demand rather than scheduled. Run it after an
    installer visit, or after adding solar, an aPower, an aGate or accessories.

    It exists because nothing refreshed the profile at all: `upsert_gateway` was
    called with a full profile only at registration, and the poll loop merely
    patched `apower_units` back in. One live install carried an April snapshot
    into September, reporting firmware V12R02B85D00_250624 while the gateway ran
    V12R02B30D06_260304, and an empty pto_date the cloud had held since
    2024-08-21. Every surface reading profile_json was showing that.

    Returns a summary including which fields changed, so the caller can say what
    actually happened rather than just "done".
    """
    from src.services import db
    from src.app_state import get_app_state

    gw = await db.get_gateway_full(short_id)
    if not gw:
        raise ValueError(f"Gateway {short_id} not found")

    # Prefer the running gateway's client — it is already authenticated and
    # shares the service's rate limiting. Fall back to building one from stored
    # credentials: refusing to refresh configuration because polling happens to
    # be stopped is an arbitrary restriction, and it makes this unusable from a
    # script or a CLI, where there is no registry at all.
    registry = get_app_state().get("registry")
    svc = registry.get_gateway(short_id) if registry else None

    before: dict = {}
    try:
        before = json.loads(gw.get("profile_json") or "{}")
    except json.JSONDecodeError:
        before = {}

    if svc is not None:
        client = await svc._get_or_create_client()
    else:
        creds = await db.get_credentials(gw.get("full_serial", ""))
        if not creds or not creds.get("email"):
            raise RuntimeError(
                "Gateway is not running and no stored credentials were found — "
                "cannot reach the cloud to refresh."
            )
        from franklinwh_cloud.client import TokenFetcher, Client
        fetcher = TokenFetcher(creds["email"], creds["password"])
        await fetcher.get_token()
        client = Client(fetcher, gateway=gw.get("full_serial", ""))
        logger.info(f"[{short_id}] refresh: gateway not running, using stored credentials")

    raw = await client.discover(tier=3)
    snapshot = dataclasses.asdict(raw) if dataclasses.is_dataclass(raw) else raw

    after = build_profile_from_snapshot(snapshot, gw, short_id)

    # Grid profile is not part of discover(tier=3); onboarding fetches it
    # separately and patches it in. Refresh must do the same or it nulls a value
    # the installer set — which it did, turning "User Defined" into null on the
    # first live run.
    try:
        _gp = await client.get_grid_profile_info(requestType=1)
        _cur = _gp.get("currentId") if isinstance(_gp, dict) else None
        _match = next((x for x in (_gp.get("list") or []) if x.get("id") == _cur), None)
        if _match:
            after["grid_profile_name"] = _match.get("name", "")
            after["grid_profile_id"] = _cur
    except Exception as exc:
        logger.debug(f"[{short_id}] grid profile unavailable on refresh: {exc!r}")

    # SPAN integration. discover() does not carry it, so it is fetched and
    # patched in the same way the grid profile is.
    #
    # `spanFlag` records that an installer CONFIGURED the SPAN integration in
    # the FranklinWH app. It is not a detection: 0 does NOT mean no panel is
    # present (DEF-SPAN-FLAG-IS-CONFIG-NOT-DETECTION). 0.6.21 shipped this as
    # `has_span` and read it as presence, which would have told an owner with an
    # unconfigured panel that they had none.
    #
    # The distinction is load-bearing: a SPAN sits between the aGate and the
    # loads, and switching such a site to WiFi can stop the panel communicating
    # while the write reports success (DEF-WIFI-SWITCH-BREAKS-SPAN).
    #
    # None on failure, which the merge below reads as "unknown" and leaves the
    # previous value standing.
    try:
        _span = await client.get_span_setting()
        _flag = (_span or {}).get("spanFlag") if isinstance(_span, dict) else None
        after["span_configured"] = bool(int(_flag)) if _flag is not None else None
    except Exception as exc:
        logger.debug(f"[{short_id}] SPAN setting unavailable on refresh: {exc!r}")
        after["span_configured"] = None

    # Never let a partial discovery destroy known-good data. A field the refresh
    # could not determine comes back None, which means "unknown", not "absent",
    # so the previous value stands. A genuine False is left alone — an aPBox
    # really can be removed, and that must still be recorded.
    for key, previous in before.items():
        if key.startswith("_"):
            continue
        if after.get(key) is None and previous not in (None, "", [], {}):
            after[key] = previous

    # Report what moved. Ignore the discovery marker itself — it changes every
    # run by definition and would drown the real differences.
    ignored = {"_discovered_at"}
    changed = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(set(before) | set(after))
        if key not in ignored and before.get(key) != after.get(key)
    }

    await db.upsert_gateway(
        short_id=short_id,
        full_serial=gw.get("full_serial", ""),
        name=gw.get("name", ""),
        site_id=gw.get("site_id", ""),
        model=gw.get("model", ""),
        profile=after,
        credentials={},
        enabled=gw.get("enabled", 1),
    )

    logger.info(
        f"[{short_id}] profile refreshed — {len(changed)} field(s) changed"
        + (f": {', '.join(list(changed)[:8])}" if changed else "")
    )
    return {
        "ok": True,
        "short_id": short_id,
        "previously_discovered_at": before.get("_discovered_at"),
        "changed_count": len(changed),
        "changed": changed,
    }
