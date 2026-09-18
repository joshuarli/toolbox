from __future__ import annotations

import json
import sys
from typing import Any

from tool.ig.browser import browser_session
from tool.ig.scraper import read_js

_FINGERPRINT_JS = read_js("fingerprint.js")


def _host_fingerprint() -> dict[str, Any]:
    import platform

    machine = platform.machine()
    system = platform.system()
    return {
        "system": system,
        "machine": machine,
        "processor": platform.processor(),
        "release": platform.release(),
        "version": platform.version(),
        "macosArm": system == "Darwin" and machine.lower() in {"arm64", "aarch64"},
    }


def _fingerprint_warnings(fp: dict[str, Any], host: dict[str, Any] | None = None) -> list[str]:
    warnings = []
    if host is not None and not host.get("macosArm"):
        warnings.append("host is not macOS ARM; Mac laptop fingerprint is not a trusted baseline")
    if fp.get("webdriver") is not None:
        warnings.append("navigator.webdriver is exposed")
    automation_globals = fp.get("automationGlobals") or []
    if automation_globals:
        warnings.append("automation globals are exposed")
    if fp.get("hidden") is not False or fp.get("visibilityState") != "visible":
        warnings.append("document visibility does not look foregrounded")
    languages = fp.get("languages") or []
    if fp.get("language") != "en-US" or languages[:2] != ["en-US", "en"]:
        warnings.append("navigator language surface is unexpected")
    if fp.get("platform") != "MacIntel":
        warnings.append("navigator.platform is unexpected")
    if (fp.get("hardwareConcurrency") or 0) < 2:
        warnings.append("hardwareConcurrency is implausibly low")
    elif host is not None and host.get("macosArm") and fp.get("hardwareConcurrency") not in {8, 10, 12, 14, 16, 20, 24}:
        warnings.append("hardwareConcurrency is unusual for Apple Silicon")
    device_memory = fp.get("deviceMemory")
    if device_memory is not None and device_memory < 4:
        warnings.append("deviceMemory is implausibly low for a Mac laptop")
    if fp.get("maxTouchPoints") != 0:
        warnings.append("maxTouchPoints is unexpected for desktop Chrome")
    if "PDF Viewer" not in (fp.get("plugins") or []):
        warnings.append("PDF plugin is missing")
    if "application/pdf" not in (fp.get("mimeTypes") or []):
        warnings.append("PDF mimeType is missing")
    if fp.get("pluginsInstance") is False or fp.get("pluginsClass") not in (None, "[object PluginArray]"):
        warnings.append("navigator.plugins does not look like a native PluginArray")
    if fp.get("mimeTypesInstance") is False or fp.get("mimeTypesClass") not in (None, "[object MimeTypeArray]"):
        warnings.append("navigator.mimeTypes does not look like a native MimeTypeArray")
    if fp.get("notificationPermission") not in {"default", "prompt"}:
        warnings.append("notification permission surface is unexpected")
    inner = fp.get("inner") or []
    outer = fp.get("outer") or []
    screen = fp.get("screen") or []
    if inner[:2] != [1920, 1080]:
        warnings.append("viewport size is unexpected")
    if len(outer) < 2 or outer[0] < inner[0] or outer[1] < inner[1]:
        warnings.append("outer window size is smaller than viewport")
    if len(screen) < 6 or screen[:2] != [1920, 1080] or screen[4:] != [24, 24]:
        warnings.append("screen metrics are unexpected")
    if fp.get("devicePixelRatio") != 1:
        warnings.append("devicePixelRatio is unexpected")
    descriptors = fp.get("descriptors") or {}
    descriptor_props = (
        "webdriver",
        "language",
        "languages",
        "platform",
        "hardwareConcurrency",
        "deviceMemory",
        "plugins",
        "mimeTypes",
    )
    for prop in descriptor_props:
        descriptor = descriptors.get(prop) or {}
        if descriptor.get("getterNative") is False:
            warnings.append(f"navigator.{prop} getter does not look native")
    user_agent = fp.get("userAgent") or ""
    if "Chrome/" not in user_agent:
        warnings.append("userAgent does not look Chrome-family")
    headers = fp.get("headers") or {}
    request_headers = headers.get("headers") if isinstance(headers, dict) else None
    if isinstance(request_headers, dict):
        header_ua = request_headers.get("User-Agent")
        header_language = request_headers.get("Accept-Language")
        if header_ua and header_ua != user_agent:
            warnings.append("request User-Agent does not match navigator.userAgent")
        if header_language and not header_language.startswith("en-US"):
            warnings.append("request Accept-Language does not match navigator.languages")
    worker = fp.get("worker") or {}
    if isinstance(worker, dict) and not worker.get("error"):
        warnings.extend(
            f"worker navigator.{key} does not match page context"
            for key in ("userAgent", "platform", "language", "hardwareConcurrency")
            if key in worker and key in fp and worker.get(key) != fp.get(key)
        )
        if worker.get("deviceMemory") is not None and worker.get("deviceMemory") != fp.get("deviceMemory"):
            warnings.append("worker navigator.deviceMemory does not match page context")
        if worker.get("maxTouchPoints") is not None and worker.get("maxTouchPoints") != fp.get("maxTouchPoints"):
            warnings.append("worker navigator.maxTouchPoints does not match page context")
        if worker.get("languages") is not None and worker.get("languages") != fp.get("languages"):
            warnings.append("worker navigator.languages does not match page context")
    elif isinstance(worker, dict) and worker.get("error"):
        warnings.append("worker fingerprint probe failed")
    if isinstance(fp.get("canvas"), dict) and fp["canvas"].get("error"):
        warnings.append("canvas fingerprint probe failed")
    if isinstance(fp.get("audio"), dict) and fp["audio"].get("error"):
        warnings.append("audio fingerprint probe failed")
    if isinstance(fp.get("mediaDevices"), dict) and fp["mediaDevices"].get("error"):
        warnings.append("media device fingerprint probe failed")
    elif host is not None and host.get("macosArm") and isinstance(fp.get("mediaDevices"), list):
        device_kinds = {device.get("kind") for device in fp["mediaDevices"] if isinstance(device, dict)}
        if "audioinput" not in device_kinds or "videoinput" not in device_kinds:
            warnings.append("media devices do not look like a Mac laptop")
    fonts = fp.get("fonts") or {}
    measured_fonts = fonts.get("measured") if isinstance(fonts, dict) else None
    base_fonts = fonts.get("base") if isinstance(fonts, dict) else None
    if host is not None and host.get("macosArm") and isinstance(measured_fonts, dict) and isinstance(base_fonts, dict):
        monospace_width = base_fonts.get("monospace")
        mac_font_widths = [measured_fonts.get("-apple-system"), measured_fonts.get("Helvetica Neue")]
        if monospace_width is not None and all(width == monospace_width for width in mac_font_widths):
            warnings.append("Mac system font probes look absent")
    webgl = fp.get("webgl")
    if not isinstance(webgl, dict) or webgl.get("error") or not webgl.get("vendor") or not webgl.get("renderer"):
        warnings.append("WebGL renderer surface is unavailable")
    elif host is not None and host.get("macosArm"):
        webgl_text = f"{webgl.get('vendor', '')} {webgl.get('renderer', '')}".lower()
        if "apple" not in webgl_text:
            warnings.append("WebGL renderer does not look Apple-backed on macOS ARM host")
        if any(term in webgl_text for term in ("software", "swiftshader", "llvmpipe")):
            warnings.append("WebGL renderer looks software-backed")
    return warnings


async def _fingerprint(*, live: bool = False, extensions: list[str] | None = None, ublock: bool = True) -> None:
    async with browser_session(headless=not live, extensions=extensions, ublock=ublock) as (_browser, tab):
        await tab.activate()
        await tab.navigate("https://example.com/", wait="complete")
        fingerprint = await tab.evaluate(_FINGERPRINT_JS)
        host = _host_fingerprint()
        fingerprint["host"] = host
        fingerprint["mode"] = "headful" if live else "headless"
        print(json.dumps(fingerprint, indent=2, sort_keys=True))
        for warning in _fingerprint_warnings(fingerprint, host):
            print(f"warning: {warning}", file=sys.stderr)
