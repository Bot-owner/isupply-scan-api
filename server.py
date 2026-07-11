
# ─── V35 ACTIVE HID REPORT PROBE ─────────────────────────────────────────────
# READ-ONLY discovery probe.
# Cíl: zjistit, zda diagnostics relay umí vrátit živý HID input/feature/vendor
# report pro SPU/ALS/prox větev. Probe neposílá SetReport ani žádný payload,
# který by zapisoval do HID zařízení.
_V35_HID_NAMES = (
    "AppleSPUHIDDevice",
    "AppleSPUHIDDriver",
    "AppleSPUHIDInterface",
    "AppleSphinxProxHIDEventDriver",
    "AppleProxDriver",
    "als",
    "prox",
)

_V35_EXPECTED_ALS = "0311133F6B07"

def _v35_safe(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        return {
            "__type__": "bytes",
            "length": len(b),
            "hex": b.hex().upper(),
            "ascii": b.decode("ascii", errors="replace").replace("\x00", "\\0"),
        }
    if isinstance(value, dict):
        return {str(k): _v35_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v35_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)

def _v35_walk(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _v35_walk(child, child_path)
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            child_path = f"{path}[{idx}]"
            yield child_path, str(idx), child
            yield from _v35_walk(child, child_path)

def _v35_forms(value):
    out = []
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="ignore")
        out.append(("string", value, raw))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        out.append(("bytes", raw.hex().upper(), raw))
    elif isinstance(value, int) and value >= 0:
        n = max(1, (value.bit_length() + 7) // 8)
        for endian in ("little", "big"):
            try:
                raw = value.to_bytes(n, endian)
                out.append((f"integer_{endian}", str(value), raw))
            except Exception:
                pass
    return out

def _v35_analyse(label, obj):
    expected = bytes.fromhex(_V35_EXPECTED_ALS)
    expected_forms = {
        "raw6": expected,
        "raw6_reversed": expected[::-1],
        "ascii": _V35_EXPECTED_ALS.encode("ascii"),
        "utf16le": _V35_EXPECTED_ALS.encode("utf-16le"),
        "utf16be": _V35_EXPECTED_ALS.encode("utf-16be"),
    }
    hits, report_candidates = [], []
    tokens = ("report", "hid", "input", "feature", "vendor", "spu",
              "prox", "als", "ambient", "sensor", "event", "value", "data")
    for path, key, value in _v35_walk(obj):
        hay = (path + "." + key).lower()
        for form, rendered, raw in _v35_forms(value):
            for transform, needle in expected_forms.items():
                pos = raw.find(needle)
                if pos >= 0:
                    hits.append({
                        "source": label, "path": path, "key": key,
                        "value_form": form, "expected_transform": transform,
                        "offset": pos, "raw_hex": raw.hex().upper(),
                    })
            if any(t in hay for t in tokens):
                report_candidates.append({
                    "source": label, "path": path, "key": key,
                    "value_form": form, "value": rendered[:4000],
                    "raw_hex": raw.hex().upper()[:8000],
                    "raw_length": len(raw),
                })
    return hits, report_candidates

async def _v35_hid_report_probe_collect(udid):
    import inspect
    import datetime as _dt
    import json as _json

    ld, diag = await _open_diag(udid)
    calls, responses, exact_hits, candidates = [], {}, [], []

    async def read(label, fn):
        try:
            obj = fn()
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({
                "source": label, "ok": True,
                "truthy": bool(obj), "result_type": type(obj).__name__,
            })
            responses[label] = _v35_safe(obj)
            if obj:
                h, c = _v35_analyse(label, obj)
                exact_hits.extend(h)
                candidates.extend(c)
            return obj
        except Exception as exc:
            calls.append({
                "source": label, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None

    # Baseline: normální IORegistry API.
    for name in _V35_HID_NAMES:
        await read(
            f"ioregistry:name:{name}",
            lambda name=name: diag.ioregistry(name=name)
        )

    # Raw diagnostics IORegistry variants. V34 potvrdila, že tyto cesty existují.
    for name in _V35_HID_NAMES:
        for selector in ("EntryName", "Name", "CurrentEntry", "RegistryEntryName"):
            payload = {"Request": "IORegistry", selector: name}
            await read(
                f"diagnostics:raw:IORegistry:{selector}:{name}",
                lambda payload=payload: diag._send_recv(payload)
            )

    # Aktivní READ probe. Záměrně pouze názvy operací typu Get/Read/Copy.
    # ŽÁDNÝ SetReport / WriteReport / OutputReport.
    read_requests = (
        "HIDReport",
        "HIDInputReport",
        "HIDFeatureReport",
        "IOHIDReport",
        "IOHIDInputReport",
        "IOHIDFeatureReport",
        "GetHIDReport",
        "GetHIDInputReport",
        "GetHIDFeatureReport",
        "ReadHIDReport",
        "ReadHIDInputReport",
        "ReadHIDFeatureReport",
        "CopyHIDReport",
        "HIDEvent",
        "HIDEvents",
        "IOHIDEvent",
        "IOHIDEvents",
    )

    # Report 0 = AppleSPUHIDDevice input report podle descriptoru.
    # 0x5A = známý ChildVendorMessage prox report.
    report_ids = (0x00, 0x5A)

    for request_name in read_requests:
        # Nejdřív čistý request: některé relay implementace ignorují selektory.
        payload = {"Request": request_name}
        await read(
            f"diagnostics:active:{request_name}",
            lambda payload=payload: diag._send_recv(payload)
        )

        for name in _V35_HID_NAMES:
            for report_id in report_ids:
                # Varianty názvů parametrů jsou discovery-only. Všechny jsou read-only.
                variants = (
                    {"Request": request_name, "EntryName": name, "ReportID": report_id},
                    {"Request": request_name, "Name": name, "ReportID": report_id},
                    {"Request": request_name, "RegistryEntryName": name, "ReportID": report_id},
                    {"Request": request_name, "Device": name, "ReportID": report_id},
                    {"Request": request_name, "Service": name, "ReportID": report_id},
                    {"Request": request_name, "EntryName": name, "ReportId": report_id},
                )
                for idx, payload in enumerate(variants):
                    await read(
                        f"diagnostics:active:{request_name}:{name}:rid{report_id:02X}:v{idx}",
                        lambda payload=payload: diag._send_recv(payload)
                    )

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    # Dedup candidates/hits.
    def dedup(rows, fields):
        seen, out = set(), []
        for row in rows:
            ident = tuple(str(row.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident)
                out.append(row)
        return out

    exact_hits = dedup(
        exact_hits,
        ("source", "path", "expected_transform", "offset", "raw_hex")
    )
    candidates = dedup(
        candidates,
        ("source", "path", "key", "value_form", "raw_hex")
    )

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v35", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    with open(os.path.join(capture_dir, "responses.json"), "w", encoding="utf-8") as fh:
        _json.dump(responses, fh, ensure_ascii=False, indent=2)

    result = {
        "ok": True,
        "probe": "active-hid-report-probe-v35",
        "read_only": True,
        "udid": udid,
        "goal": "Read live HID/input/feature/vendor report data from SPU ALS/prox path",
        "expected_als": _V35_EXPECTED_ALS,
        "target_report_ids": ["0x00", "0x5A"],
        "write_requests_sent": 0,
        "exact_hits": exact_hits,
        "report_candidates": candidates[:5000],
        "calls": calls,
        "summary": {
            "calls_total": len(calls),
            "calls_ok": sum(1 for x in calls if x.get("ok")),
            "calls_truthy": sum(1 for x in calls if x.get("truthy")),
            "exact_hits": len(exact_hits),
            "report_candidates": len(candidates),
        },
        "capture_dir": capture_dir,
        "files": {"responses": "responses.json"},
        "conclusion": (
            "If an active diagnostics HID request returns live report bytes, inspect "
            "exact_hits and report_candidates. If all active request names are rejected "
            "or return only IORegistry metadata, diagnostics relay does not expose "
            "IOHIDDeviceGetReport directly and the next step is reproducing the exact "
            "private request observed from 3uTools traffic/runtime instrumentation."
        ),
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        _json.dump(result, fh, ensure_ascii=False, indent=2)

    return result

@app.route('/api/v35-hid-report-probe/<udid>', methods=['GET'])
def api_v35_hid_report_probe(udid):
    try:
        result = _run_async_isolated(
            _v35_hid_report_probe_collect(udid),
            timeout=900
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "active-hid-report-probe-v35",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200
