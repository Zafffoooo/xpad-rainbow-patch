#!/usr/bin/env python3
"""Temporarily substitute XPAD's early Chillstream VID/PID in VSH RAM.

This deliberately supports one narrowly-scoped patch only:
    046d:c242 -> 413d:2104

It uses webMAN MOD's documented HTTP PS3MAPI endpoints.  It never touches
disk on the PS3, boot plugin lists, LV1, LV2, or any process other than VSH.
Run with --dry-run first.  A real write also requires --patch and the explicit
--confirm-dongle-disconnected acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


XPAD_PATH = "/dev_hdd0/plugins/xpad.sprx"
EXPECTED = bytes.fromhex("04 6D C2 42")
REPLACEMENT = bytes.fromhex("41 3D 21 04")
# webMAN's HTML /getmem.ps3mapi viewer limits a single response to 0x100
# bytes. Keep every request within that physical limit.
READ_CHUNK = 0x100
MAX_DATA_SEGMENT_FILE_SIZE = 0x40000
HTTP_TIMEOUT_SECONDS = 2.0
# A real patch must leave margin before XPAD's source-level 10 s startup
# delay ends.  Dry-runs still report their timing but never write.
MAX_REAL_PATCH_ELAPSED_SECONDS = 8.0
TRACE_RESPONSE_LIMIT = 8192


class PatchAbort(RuntimeError):
    """A guarded precondition failed; no write must follow."""


@dataclass(frozen=True)
class Segment:
    label: str
    base: int
    filesz: int
    memsz: int
    index: int
    seg_type: int

    @property
    def end(self) -> int:
        return self.base + self.filesz


@dataclass(frozen=True)
class ModuleSegments:
    name: str
    filename: str
    filename_size: int
    modattribute: int
    start_entry: int
    stop_entry: int
    segments: tuple[Segment, ...]


class Webman:
    def __init__(self, host: str, port: int, trace_api: bool = False) -> None:
        self.base = f"http://{host}:{port}"
        self.trace_api = trace_api

    def trace_before_parse(self, kind: str, request: str, parser: str, raw: str) -> None:
        """Emit response provenance without changing request/parser behavior."""
        if not getattr(self, "trace_api", False):
            return
        shown = raw
        if len(shown) > TRACE_RESPONSE_LIMIT:
            shown = shown[:TRACE_RESPONSE_LIMIT] + f"... [truncated; {len(raw)} chars total]"
        print(f"REQUEST KIND: {kind}")
        print(f"REQUEST: {request}")
        print(f"PARSER: {parser}")
        print(f"RAW RESPONSE: {shown}")

    def trace(self, message: str) -> None:
        if getattr(self, "trace_api", False):
            print(message)

    def get(self, path: str, query: dict[str, str] | None = None) -> str:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, safe="/")
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
                if response.status != 200:
                    raise PatchAbort(f"HTTP {response.status} for {path}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PatchAbort(f"HTTP failure for {url}: {exc}") from exc
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchAbort(f"non-text HTTP response for {path}") from exc

    @staticmethod
    def parse_object(body: str) -> dict[str, Any]:
        """Parse webMAN's HTTP PS3MAPI response.

        webMAN's current source serializes ordinary strings as valid JSON, but
        serializes some numeric response arrays with bare ``0x...`` tokens.
        Convert only those tokens outside quoted JSON strings, then require an
        object containing the documented ``response`` member.
        """
        text = body.strip()
        if not text.startswith("{") or not text.endswith("}"):
            raise PatchAbort(f"unexpected PS3MAPI response: {text[:160]!r}")

        out: list[str] = []
        i = 0
        in_string = False
        escaped = False
        while i < len(text):
            char = text[i]
            if in_string:
                out.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                i += 1
                continue
            if char == '"':
                in_string = True
                out.append(char)
                i += 1
                continue
            match = re.match(r"0x([0-9A-Fa-f]+)", text[i:])
            if match:
                out.append(str(int(match.group(1), 16)))
                i += len(match.group(0))
                continue
            out.append(char)
            i += 1

        try:
            value = json.loads("".join(out))
        except json.JSONDecodeError as exc:
            raise PatchAbort(f"unparseable PS3MAPI response: {text[:160]!r}") from exc
        if not isinstance(value, dict):
            raise PatchAbort(f"PS3MAPI response is not an object: {value!r}")
        return value

    @classmethod
    def response_object(cls, body: str) -> dict[str, Any]:
        value = cls.parse_object(body)
        if "response" not in value:
            raise PatchAbort(f"PS3MAPI response lacks response member: {value!r}")
        return value

    @staticmethod
    def extract_response_array_body(body: str) -> str:
        """Return only the strict ``response`` array body, without parsing it."""
        match = re.fullmatch(r'\s*\{\s*"response"\s*:\s*\[(.*)\]\s*\}\s*', body, re.DOTALL)
        if match is None:
            raise PatchAbort("numeric-array response does not have exact {\"response\":[...]} form")
        return match.group(1).strip()

    @staticmethod
    def parse_numeric_array_body(array_body: str) -> list[int]:
        """Strictly parse comma-separated unsigned decimal/hex scalar values."""
        if not array_body:
            return []
        if array_body.endswith(","):
            array_body = array_body[:-1].strip()
            if not array_body:
                raise PatchAbort("numeric-array trailing comma has no preceding scalar")

        result: list[int] = []
        for token in array_body.split(","):
            token = token.strip()
            if not re.fullmatch(r"(?:0|[1-9][0-9]*|0[xX][0-9A-Fa-f]+)", token):
                raise PatchAbort(f"invalid numeric-array token: {token!r}")
            try:
                result.append(int(token, 0))
            except ValueError as exc:
                raise PatchAbort(f"invalid numeric-array integer: {token!r}") from exc
        return result

    @classmethod
    def parse_numeric_response_array(cls, body: str) -> list[int]:
        """Strictly parse webMAN's non-standard numeric response arrays.

        Current webMAN PS3MAPI emits arrays such as
        ``{"response": [0,0x10003000,0,]}``: bare hexadecimal literals and
        a final comma make this deliberately unsuitable for ``json.loads``.
        This parser accepts only that one outer-object shape and unsigned
        decimal/``0x`` scalar elements. It accepts one optional trailing comma
        and rejects strings, nested values, missing elements, and extra object
        members.
        """
        return cls.parse_numeric_array_body(cls.extract_response_array_body(body))

    @staticmethod
    def parse_module_id_response_array(body: str) -> list[int]:
        """Parse MODULE GETALLPRXID without weakening generic array parsing.

        Current upstream webMAN source appends a pipe after zero module IDs,
        but appends a nonzero HTTP module ID as ``0x%x`` *without* a pipe.
        On the physical target that yields a hybrid HTTP array: its first
        field is a concatenation of fixed-width ``0xXXXXXXXX`` IDs and later
        fields are ordinary zero placeholders. Accept that form only here.
        """
        array_body = Webman.extract_response_array_body(body)
        if not array_body:
            return []
        if array_body.endswith(","):
            array_body = array_body[:-1].strip()
            if not array_body:
                raise PatchAbort("module-ID array trailing comma has no preceding field")

        result: list[int] = []
        for field in array_body.split(","):
            field = field.strip()
            if not field:
                raise PatchAbort("module-ID array has an empty non-trailing field")

            # The webMAN formatter emits nonzero IDs as 0x%x without a
            # separator, but emits a following zero as "0|". Thus its final
            # nonzero ID plus first zero can be one field: 0xXXXXXXXX0.
            # Consume exact eight-hex-digit IDs only; a sole literal trailing
            # zero is the only permitted remainder.
            if field.startswith("0x"):
                cursor = 0
                ids: list[int] = []
                while re.match(r"0x[0-9A-Fa-f]{8}", field[cursor:]):
                    ids.append(int(field[cursor + 2 : cursor + 10], 16))
                    cursor += 10
                if ids:
                    remainder = field[cursor:]
                    if remainder == "":
                        result.extend(ids)
                        continue
                    if remainder == "0":
                        result.extend(ids)
                        result.append(0)
                        continue
                    raise PatchAbort(f"invalid module-ID concatenation remainder: {remainder!r}")

            if re.fullmatch(r"(?:0|[1-9][0-9]*|0[xX][0-9A-Fa-f]+)", field):
                try:
                    result.append(int(field, 0))
                except ValueError as exc:
                    raise PatchAbort(f"invalid module-ID numeric field: {field!r}") from exc
                continue
            raise PatchAbort(f"invalid module-ID array field: {field!r}")
        return result

    @staticmethod
    def parse_module_segments_response(body: str) -> ModuleSegments:
        """Strict parser for webMAN's malformed MODULE GETSEGMENTS response.

        webMAN formats the six module metadata fields as name, filename,
        filename_size, modattribute, start_entry and stop_entry. It then emits
        four groups of segment label, base, filesz, memsz, index and type.
        Its formatter omits the separator between a group's final ``type`` and
        the next ``"segN"`` label, so this parser consumes groups sequentially
        rather than applying a global string replacement.
        """
        # webMAN uses %i for every metadata value.  Unlike segment values,
        # they are decimal signed integers in the formatter, not hex tokens.
        signed_decimal = r"-?(?:0|[1-9][0-9]*)"
        string = r'"([^"\\]*)"'
        metadata = re.compile(
            rf'\s*\{{\s*"response"\s*:\s*\[\s*{string}\s*,\s*{string}\s*,\s*'
            rf'({signed_decimal})\s*,\s*({signed_decimal})\s*,\s*'
            rf'({signed_decimal})\s*,\s*({signed_decimal})\s*,',
            re.DOTALL,
        )
        match = metadata.match(body)
        if match is None:
            raise PatchAbort("MODULE GETSEGMENTS metadata does not match the exact webMAN layout")
        name, filename, filename_size, modattribute, start_entry, stop_entry = match.groups()

        def parse_metadata_int(token: str, field: str) -> int:
            try:
                return int(token, 10)
            except ValueError as exc:
                raise PatchAbort(f"MODULE GETSEGMENTS invalid {field}: {token!r}") from exc

        cursor = match.end()
        segment_re = re.compile(
            r'\s*"seg([0-9]+)"\s*,\s*'
            r'(0x[0-9A-Fa-f]+)\s*,\s*'
            r'(0x[0-9A-Fa-f]+)\s*,\s*'
            r'(0x[0-9A-Fa-f]+)\s*,\s*'
            r'(0x[0-9A-Fa-f]+)\s*,\s*'
            r'(0x[0-9A-Fa-f]+)',
            re.DOTALL,
        )
        segments: list[Segment] = []
        for expected_label in range(4):
            segment_match = segment_re.match(body, cursor)
            if segment_match is None:
                raise PatchAbort(f"MODULE GETSEGMENTS missing/malformed seg{expected_label}")
            label_number, base, filesz, memsz, index, seg_type = segment_match.groups()
            if int(label_number, 10) != expected_label:
                raise PatchAbort(
                    f"MODULE GETSEGMENTS expected seg{expected_label}, got seg{label_number}"
                )
            segments.append(
                Segment(
                    label=f"seg{label_number}",
                    base=int(base, 16),
                    filesz=int(filesz, 16),
                    memsz=int(memsz, 16),
                    index=int(index, 16),
                    seg_type=int(seg_type, 16),
                )
            )
            cursor = segment_match.end()

        if not re.fullmatch(r'\s*\]\s*\}\s*', body[cursor:], re.DOTALL):
            raise PatchAbort("MODULE GETSEGMENTS has trailing garbage or an unexpected extra segment")
        return ModuleSegments(
            name=name,
            filename=filename,
            filename_size=parse_metadata_int(filename_size, "filename_size"),
            modattribute=parse_metadata_int(modattribute, "modattribute"),
            start_entry=parse_metadata_int(start_entry, "start_entry"),
            stop_entry=parse_metadata_int(stop_entry, "stop_entry"),
            segments=tuple(segments),
        )

    def command(self, command: str) -> Any:
        # /ps3mapi.ps3?<PS3 MAPI COMMAND> is the documented JSON interface.
        path = "/ps3mapi.ps3?" + urllib.parse.quote(command, safe="")
        raw = self.get(path)
        self.trace_before_parse(command, path, "Webman.response_object", raw)
        return self.response_object(raw)["response"]

    def command_numeric_array(self, command: str) -> list[int]:
        path = "/ps3mapi.ps3?" + urllib.parse.quote(command, safe="")
        raw = self.get(path)
        self.trace_before_parse(command, path, "Webman.parse_numeric_response_array", raw)
        return self.parse_numeric_response_array(raw)

    def command_module_id_array(self, command: str) -> list[int]:
        path = "/ps3mapi.ps3?" + urllib.parse.quote(command, safe="")
        raw = self.get(path)
        self.trace_before_parse(command, path, "Webman.parse_module_id_response_array", raw)
        return self.parse_module_id_response_array(raw)

    def command_module_segments(self, command: str) -> ModuleSegments:
        path = "/ps3mapi.ps3?" + urllib.parse.quote(command, safe="")
        raw = self.get(path)
        self.trace_before_parse(command, path, "Webman.parse_module_segments_response", raw)
        return self.parse_module_segments_response(raw)

    @staticmethod
    def parse_memory_get_response(body: str) -> bytes:
        """Strictly extract one hex payload from webMAN's HTML memory viewer.

        /getmem.ps3mapi does not use the JSON PS3MAPI response convention. It
        embeds its raw memory payload only in a hidden textarea named output.
        Do not inspect or parse rendered dump rows: they are presentation, not
        the returned memory data.
        """
        if re.search(r"<html\b", body, re.IGNORECASE) is None:
            raise PatchAbort("MEMORY GET did not return an HTML response")
        output_textareas = list(
            re.finditer(
                r'<textarea\s+id="output"(?:\s+[^>]*)?>(.*?)</textarea\s*>',
                body,
                re.IGNORECASE | re.DOTALL,
            )
        )
        if len(output_textareas) != 1:
            raise PatchAbort(
                f"MEMORY GET requires exactly one textarea id=output, got {len(output_textareas)}"
            )
        payload = output_textareas[0].group(1).strip()
        if not re.fullmatch(r"[0-9A-Fa-f]+", payload):
            raise PatchAbort("MEMORY GET output textarea is missing or contains non-hex data")
        if len(payload) % 2:
            raise PatchAbort("MEMORY GET output textarea has an odd number of hex characters")
        try:
            return bytes.fromhex(payload)
        except ValueError as exc:
            raise PatchAbort("MEMORY GET output textarea contains invalid hex") from exc

    @staticmethod
    def parse_setmem_submission_response(body: str) -> None:
        """Accept only webMAN's normal HTML page as SETMEM submission output.

        The legacy /setmem.ps3mapi web command is not the JSON PS3MAPI
        endpoint. Its HTTP completion means only that the one request was
        submitted. The subsequent fresh MEMORY GET read-back is the sole proof
        that this program accepts as a successful patch.
        """
        if re.search(r"<html\b", body, re.IGNORECASE) is None:
            raise PatchAbort("SETMEM did not return webMAN's expected HTML response")

    def load_vsh_plugin(self, slot: int) -> None:
        # This endpoint is documented to load into the given VSH slot.
        self.get("/loadprx.ps3", {"slot": str(slot), "prx": XPAD_PATH})

    def read_memory(self, pid: int, address: int, length: int) -> bytes:
        if length <= 0 or length > READ_CHUNK:
            raise PatchAbort(f"invalid bounded memory-read length: {length}")
        query = {"proc": str(pid), "addr": f"0x{address:X}", "len": str(length)}
        raw = self.get("/getmem.ps3mapi", query)
        request = "/getmem.ps3mapi?" + urllib.parse.urlencode(query)
        self.trace_before_parse("MEMORY GET", request, "Webman.parse_memory_get_response", raw)
        data = self.parse_memory_get_response(raw)
        if len(data) != length:
            raise PatchAbort(
                f"MEMORY GET length mismatch at 0x{address:X}: expected {length}, got {len(data)}"
            )
        return data

    def write_memory(self, pid: int, address: int, data: bytes) -> None:
        if data != REPLACEMENT or len(data) != 4:
            raise PatchAbort("refusing any write other than the fixed four-byte Rainbow VID/PID")
        query = {"proc": str(pid), "addr": f"0x{address:X}", "val": data.hex().upper()}
        raw = self.get("/setmem.ps3mapi", query)
        request = "/setmem.ps3mapi?" + urllib.parse.urlencode(query)
        self.trace_before_parse("MEMORY SET", request, "Webman.parse_setmem_submission_response", raw)
        self.parse_setmem_submission_response(raw)


def values(response: Any, context: str) -> list[Any]:
    if not isinstance(response, list):
        raise PatchAbort(f"{context}: expected response array, got {response!r}")
    return response


def as_int(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise PatchAbort(f"{context}: boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0x)?[0-9A-Fa-f]+", value):
        return int(value, 16) if value.lower().startswith("0x") else int(value, 10)
    raise PatchAbort(f"{context}: invalid integer {value!r}")


def parse_current_pid_response(response: Any) -> int:
    """Parse only the documented hex string returned by GETCURRENTPID."""
    if not isinstance(response, str) or not re.fullmatch(r"0[xX][0-9A-Fa-f]{1,16}", response):
        raise PatchAbort(f"PROCESS GETCURRENTPID returned invalid PID: {response!r}")
    pid = int(response, 16)
    if not 0 < pid <= 0xFFFFFFFF:
        raise PatchAbort(f"PROCESS GETCURRENTPID returned out-of-range PID: {response!r}")
    return pid


def find_vsh_pid(webman: Webman) -> int:
    # GETALLPID exposes a representation that is not usable as the PID for
    # GETNAME on this HEN/webMAN setup. GETCURRENTPID is exact because this
    # HTTP request executes in VSH while the test is run from XMB.
    pid = parse_current_pid_response(webman.command("PROCESS GETCURRENTPID"))
    name = webman.command(f"PROCESS GETNAME {pid}")
    if not isinstance(name, str):
        raise PatchAbort(f"PROCESS GETNAME {pid}: expected string, got {name!r}")
    if "main_vsh.self" not in name.strip().lower():
        raise PatchAbort(f"current process is not VSH: PID {pid}, name {name!r}")
    return pid


def slot_is_empty(webman: Webman, slot: int) -> None:
    info = get_vsh_slot_info(webman, slot)
    # Cobra/webMAN reports an unoccupied slot as NULL. Refuse every other
    # state so /loadprx.ps3 never displaces an unrelated VSH plugin.
    if not all(item.strip().upper() in {"", "NULL"} for item in info):
        raise PatchAbort(f"VSH slot {slot} is occupied: {info!r}; unload it manually first")


def get_vsh_slot_info(webman: Webman, slot: int) -> list[str]:
    info = values(webman.command(f"MODULE GETVSHPLUGINFO {slot}"), "MODULE GETVSHPLUGINFO")
    if len(info) != 2 or not all(isinstance(item, str) for item in info):
        raise PatchAbort(f"unexpected VSH slot {slot} response: {info!r}")
    return info


def module_ids(webman: Webman, vsh_pid: int) -> list[int]:
    result = webman.command_module_id_array(f"MODULE GETALLPRXID {vsh_pid}")
    return sorted(set(item for item in result if item != 0))


def run_parser_self_tests() -> None:
    valid_cases = {
        '{"response":[0,0,0]}': [0, 0, 0],
        '{"response":[0,0x10003000,0,]}': [0, 0x10003000, 0],
        '{"response": [ 0, 0xABCDEF, 15, ]}': [0, 0xABCDEF, 15],
    }
    malformed_cases = (
        '{"response":[0,,1]}',
        '{"response":[,]}',
        '{"response":[0,"1",2]}',
        '{"response":[0xBADG]}',
        '{"response":{"pid":1}}',
        '{"response":[0],"other":1}',
        '[0,1,2]',
    )
    for source, expected in valid_cases.items():
        actual = Webman.parse_numeric_response_array(source)
        if actual != expected:
            raise AssertionError(f"parser result {actual!r} != {expected!r} for {source!r}")
    for source in malformed_cases:
        try:
            Webman.parse_numeric_response_array(source)
        except PatchAbort:
            continue
        raise AssertionError(f"malformed numeric array was accepted: {source!r}")

    module_source = '{"response":[0x230012030x230009060x231419160,0,0,]}'
    expected_module_ids = [0x23001203, 0x23000906, 0x23141916, 0, 0, 0]
    if Webman.parse_module_id_response_array(module_source) != expected_module_ids:
        raise AssertionError("concatenated module-ID parse failed")

    class OfflineModuleWebman(Webman):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        def get(self, path: str, query: dict[str, str] | None = None) -> str:
            self.calls.append((path, query))
            return module_source

    offline_webman = OfflineModuleWebman()
    if offline_webman.command_module_id_array("MODULE GETALLPRXID 16777984") != expected_module_ids:
        raise AssertionError("live MODULE GETALLPRXID dispatch path did not parse concatenated IDs")
    if offline_webman.calls != [("/ps3mapi.ps3?MODULE%20GETALLPRXID%2016777984", None)]:
        raise AssertionError(f"unexpected MODULE GETALLPRXID request path: {offline_webman.calls!r}")
    for source in (
        '{"response":[0x230012030x2300090]}',
        '{"response":[0x230012030x2300090,]}',
        '{"response":[0x23001203x23000906]}',
        '{"response":[0x230012030x2300090G]}',
        '{"response":[15x23001203]}',
        '{"response":[0x230012030x23000906,,0]}',
        '{"response":[0x230012030x23000906A]}',
        '{"response":[0x230012030x230009061]}',
    ):
        try:
            Webman.parse_module_id_response_array(source)
        except PatchAbort:
            continue
        raise AssertionError(f"malformed concatenated module IDs were accepted: {source!r}")

    class OfflineXPADDiscovery:
        trace_api = False

        def trace(self, message: str) -> None:
            pass

        def command(self, command: str) -> Any:
            if command == "MODULE GETVSHPLUGINFO 6":
                return ["XPADD", XPAD_PATH]
            if command == f"MODULE GETFILENAME 16777984 {0x230F0E14}":
                return "XPADD"
            raise AssertionError(f"unexpected discovery command: {command!r}")

        def command_module_id_array(self, command: str) -> list[int]:
            if command == "MODULE GETALLPRXID 16777984":
                return [0x23000001, 0x230F0E14, 0, 0]
            raise AssertionError(f"unexpected discovery module-ID command: {command!r}")

    discovery = OfflineXPADDiscovery()
    if identify_new_xpad_module(discovery, 16777984, 6, [0x23000001]) != 0x230F0E14:
        raise AssertionError("new-ID XPAD discovery failed")

    if parse_current_pid_response("0x0000000001000300") != 0x01000300:
        raise AssertionError("GETCURRENTPID hexadecimal parse failed")
    for source in ("01000300", "0x10003000x", "", 0x01000300):
        try:
            parse_current_pid_response(source)
        except PatchAbort:
            continue
        raise AssertionError(f"malformed current PID was accepted: {source!r}")

    # This is the HTTP shape physically captured from MODULE GETSEGMENTS.
    # webMAN's formatter omits a delimiter after each segment's final type,
    # so the next quoted segN label follows immediately.
    segments_source = (
        '{"response": ["XPADD","/dev_hdd0/plugins/xpad.sprx",512,0,24445712,24445808,'
        '"seg0",0x0000000000B30000,0x0000000000006908,0x0000000000006908,'
        '0x0000000000000000,0x0000000000000001'
        '"seg1",0x0000000001750000,0x0000000000000638,0x0000000000002D2C,'
        '0x0000000000000001,0x0000000000000001'
        '"seg2",0x0000000000000000,0x0000000000000000,0x0000000000000000,'
        '0x0000000000000002,0x0000000000000001'
        '"seg3",0x0000000000000000,0x0000000000000000,0x0000000000000000,'
        '0x0000000000000003,0x0000000000000001]}'
    )
    parsed_segments = Webman.parse_module_segments_response(segments_source)
    if (
        parsed_segments.name != "XPADD"
        or parsed_segments.filename != XPAD_PATH
        or parsed_segments.filename_size != 512
        or parsed_segments.modattribute != 0
        or parsed_segments.start_entry != 24445712
        or parsed_segments.stop_entry != 24445808
        or parsed_segments.segments[1]
        != Segment("seg1", 0x01750000, 0x638, 0x2D2C, 1, 1)
    ):
        raise AssertionError("MODULE GETSEGMENTS physical-shape parse failed")
    for source in (
        segments_source.replace('"seg1"', '"seg0"', 1),
        segments_source.replace('0x0000000000000638', '0xBADG', 1),
        segments_source[:-2] + ',"unexpected"]}',
        segments_source.replace('"seg3"', '"seg4"', 1),
    ):
        try:
            Webman.parse_module_segments_response(source)
        except PatchAbort:
            continue
        raise AssertionError(f"malformed MODULE GETSEGMENTS response was accepted: {source!r}")

    memory_source = (
        '<!DOCTYPE html><html><body><textarea id="output" style="display:none">\n'
        '046DC242\n</textarea></body></html>'
    )
    if Webman.parse_memory_get_response(memory_source) != EXPECTED:
        raise AssertionError("MEMORY GET HTML payload parse failed")

    class OfflineMemoryWebman(Webman):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        def get(self, path: str, query: dict[str, str] | None = None) -> str:
            self.calls.append((path, query))
            return memory_source

    offline_memory = OfflineMemoryWebman()
    if offline_memory.read_memory(16777984, 0x017500A8, 4) != EXPECTED:
        raise AssertionError("MEMORY GET live parser dispatch path failed")
    if offline_memory.calls != [
        ("/getmem.ps3mapi", {"proc": "16777984", "addr": "0x17500A8", "len": "4"})
    ]:
        raise AssertionError(f"unexpected MEMORY GET request path: {offline_memory.calls!r}")
    try:
        offline_memory.read_memory(16777984, 0x017500A8, 3)
    except PatchAbort:
        pass
    else:
        raise AssertionError("MEMORY GET accepted a truncated response")

    class OfflineChunkWebman:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        def read_memory(self, pid: int, address: int, length: int) -> bytes:
            self.calls.append((pid, address, length))
            return bytes([address & 0xFF]) * length

    chunk_webman = OfflineChunkWebman()
    chunk_segment = Segment("seg1", 0x01750000, 0x638, 0x2D2C, 1, 1)
    if len(read_segment(chunk_webman, 16777984, chunk_segment)) != 0x638:
        raise AssertionError("chunked segment read returned the wrong total length")
    expected_chunks = [
        (16777984, 0x01750000 + offset, length)
        for offset, length in (
            (0x000, 0x100),
            (0x100, 0x100),
            (0x200, 0x100),
            (0x300, 0x100),
            (0x400, 0x100),
            (0x500, 0x100),
            (0x600, 0x038),
        )
    ]
    if chunk_webman.calls != expected_chunks:
        raise AssertionError(f"unexpected MEMORY GET chunk sequence: {chunk_webman.calls!r}")
    for source in (
        '<html><body></body></html>',
        '<html><textarea id="output">04GG</textarea></html>',
        '<html><textarea id="output">0</textarea></html>',
        '<html><textarea id="output">04 6D</textarea></html>',
        '<html><textarea id="output">04</textarea><textarea id="output">6D</textarea></html>',
        '<html><textarea id="other">046D</textarea></html>',
    ):
        try:
            Webman.parse_memory_get_response(source)
        except PatchAbort:
            continue
        raise AssertionError(f"malformed MEMORY GET response was accepted: {source!r}")

    setmem_source = '<!DOCTYPE html><html><body>webMAN</body></html>'
    Webman.parse_setmem_submission_response(setmem_source)
    try:
        Webman.parse_setmem_submission_response('{"code":226}')
    except PatchAbort:
        pass
    else:
        raise AssertionError("SETMEM accepted a non-HTML response")

    class OfflineSetMemoryWebman(Webman):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        def get(self, path: str, query: dict[str, str] | None = None) -> str:
            self.calls.append((path, query))
            return setmem_source

    offline_setmem = OfflineSetMemoryWebman()
    offline_setmem.write_memory(16777984, 0x017500A8, REPLACEMENT)
    if offline_setmem.calls != [
        (
            "/setmem.ps3mapi",
            {"proc": "16777984", "addr": "0x17500A8", "val": "413D2104"},
        )
    ]:
        raise AssertionError(f"unexpected SETMEM request path: {offline_setmem.calls!r}")


def identify_new_xpad_module(webman: Webman, vsh_pid: int, slot: int, before_ids: list[int]) -> int:
    slot_info = get_vsh_slot_info(webman, slot)
    webman.trace(f"slot {slot} after load: {slot_info!r}")
    # This API exposes {module name, path}; require both pieces rather than
    # trusting the delta alone.
    if slot_info[0].strip().upper() != "XPADD" or slot_info[1].strip() != XPAD_PATH:
        raise PatchAbort(f"VSH slot {slot} does not identify the expected XPAD plugin: {slot_info!r}")

    after_ids = module_ids(webman, vsh_pid)
    new_ids = sorted(set(after_ids) - set(before_ids))
    webman.trace(f"PRX IDs after load: {len(after_ids)}")
    webman.trace(f"new PRX IDs: {[f'0x{module_id:08X}' for module_id in new_ids]}")
    if len(new_ids) != 1:
        raise PatchAbort(f"expected exactly one new VSH PRX ID after XPAD load, got {new_ids!r}")

    module_id = new_ids[0]
    # Physical webMAN evidence shows GETFILENAME returns XPADD (the module
    # name) for this PRX, rather than the source disk path.
    module_name = webman.command(f"MODULE GETFILENAME {vsh_pid} {module_id}")
    if module_name != "XPADD":
        raise PatchAbort(f"new PRX ID 0x{module_id:08X} is not XPADD: {module_name!r}")
    webman.trace(f"XPAD module ID: 0x{module_id:08X}")
    return module_id


def get_writable_data_segment(webman: Webman, vsh_pid: int, module_id: int) -> Segment:
    response = webman.command_module_segments(f"MODULE GETSEGMENTS {vsh_pid} {module_id}")
    if response.name != "XPADD" or response.filename != XPAD_PATH:
        raise PatchAbort(
            "MODULE GETSEGMENTS does not describe the already-identified XPAD module: "
            f"{response.name!r}, {response.filename!r}"
        )
    segments = [segment for segment in response.segments if segment.base != 0 and segment.filesz != 0]

    # scetool inspection of this supplied xpad.sprx shows PRX program header
    # 0 = executable LOAD and 1 = writable LOAD.  Require exactly that data
    # header rather than guessing based on an address or scanning all VSH.
    candidates = [segment for segment in segments if segment.index == 1]
    if len(candidates) != 1:
        raise PatchAbort(f"cannot identify exactly one XPAD writable data segment: {segments!r}")
    data = candidates[0]
    if data.memsz < data.filesz or data.filesz <= 0 or data.filesz > MAX_DATA_SEGMENT_FILE_SIZE:
        raise PatchAbort(f"unsafe XPAD data-segment size: {data!r}")
    return data


def read_segment(webman: Webman, pid: int, segment: Segment) -> bytes:
    """Read the entire file-backed data region in <= 0x100-byte requests."""
    data = bytearray()
    cursor = segment.base
    while cursor < segment.end:
        length = min(READ_CHUNK, segment.end - cursor)
        data.extend(webman.read_memory(pid, cursor, length))
        cursor += length
    return bytes(data)


def find_exactly_once(data: bytes, segment: Segment) -> int:
    matches: list[int] = []
    start = 0
    while True:
        found = data.find(EXPECTED, start)
        if found < 0:
            break
        matches.append(found)
        start = found + 1
    if len(matches) != 1:
        raise PatchAbort(f"expected exactly one {EXPECTED.hex().upper()} match in XPAD data, found {len(matches)}")
    address = segment.base + matches[0]
    if not (segment.base <= address and address + len(REPLACEMENT) <= segment.end):
        raise PatchAbort("computed patch address lies outside the XPAD writable data segment")
    return address


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ps3_ip", nargs="?", help="PS3 LAN address, e.g. 192.168.1.41")
    parser.add_argument("--slot", type=int, default=6, help="empty VSH slot to use (default: 6)")
    parser.add_argument("--port", type=int, default=80, help="webMAN HTTP port (default: 80)")
    parser.add_argument("--self-test", action="store_true", help="run offline numeric-array parser tests and exit")
    parser.add_argument("--trace-api", action="store_true", help="print each PS3MAPI request, parser, and raw response before parsing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="load/discover/read/verify, but never write (default)")
    mode.add_argument("--patch", action="store_true", help="permit the one guarded four-byte VSH-memory write")
    parser.add_argument(
        "--confirm-dongle-disconnected",
        action="store_true",
        help="required with --patch; confirms the Rainbow dongle is physically disconnected",
    )
    args = parser.parse_args()

    if args.self_test:
        run_parser_self_tests()
        print("offline numeric-array parser tests passed")
        return 0
    if not args.ps3_ip:
        parser.error("ps3_ip is required unless --self-test is used")

    if not 1 <= args.slot <= 6:
        raise PatchAbort("--slot must be in the VSH plugin range 1..6")
    if args.patch and not args.confirm_dongle_disconnected:
        raise PatchAbort("--patch requires --confirm-dongle-disconnected")
    if not args.patch and args.confirm_dongle_disconnected:
        raise PatchAbort("--confirm-dongle-disconnected is meaningful only with --patch")

    webman = Webman(args.ps3_ip, args.port, args.trace_api)
    # These are all read-only PS3MAPI operations until the final guarded write.
    vsh_pid = find_vsh_pid(webman)
    slot_is_empty(webman, args.slot)
    before_ids = module_ids(webman, vsh_pid)
    webman.trace(f"PRX IDs before load: {len(before_ids)}")
    started = time.monotonic()
    webman.load_vsh_plugin(args.slot)
    module_id = identify_new_xpad_module(webman, vsh_pid, args.slot, before_ids)
    segment = get_writable_data_segment(webman, vsh_pid, module_id)
    segment_data = read_segment(webman, vsh_pid, segment)
    address = find_exactly_once(segment_data, segment)
    target_offset = address - segment.base
    target_entry = segment_data[target_offset : target_offset + 8]
    if len(target_entry) != 8:
        raise PatchAbort("target VID/PID is not followed by its complete eight-byte XPAD table entry")

    # Keep diagnostics inside the already-read file-backed segment and centre
    # the target where possible. For the observed +0xA8 table entry, this is
    # the preceding eight bytes plus the complete eight-byte target entry.
    if len(segment_data) < 16:
        raise PatchAbort("XPAD file-backed data segment is too small for 16-byte diagnostics")
    surrounding_start = min(max(target_offset - 8, 0), len(segment_data) - 16)
    surrounding = segment_data[surrounding_start : surrounding_start + 16]
    if len(surrounding) != 16:
        raise PatchAbort("could not derive a 16-byte in-segment diagnostic window")

    # Preserve the immediate 16-byte pre-write verification, without reading
    # outside the dynamically discovered file-backed segment.
    if address + 16 > segment.end:
        raise PatchAbort("target is too near the end of XPAD file-backed data for verification")
    before = webman.read_memory(vsh_pid, address, 16)
    if before[:4] != EXPECTED:
        raise PatchAbort(f"pre-write mismatch at 0x{address:X}: {before[:4].hex(' ').upper()}")

    elapsed_ms = (time.monotonic() - started) * 1000.0
    print(f"VSH PID:                 {vsh_pid}")
    print(f"XPAD PRX/module ID:      {module_id}")
    print(f"XPAD writable segment:   0x{segment.base:X}-0x{segment.end:X} (index={segment.index}, type={segment.seg_type})")
    print(f"bytes read: 0x{len(segment_data):X}")
    print("target matches: 1")
    print(f"target address: 0x{address:X}")
    print(f"target entry bytes: {target_entry.hex(' ').upper()}")
    print(f"surrounding bytes: {surrounding.hex(' ').upper()}")
    print(f"before: {before[:8].hex(' ').upper()}")

    if not args.patch:
        print(f"DRY RUN: no memory write performed. Elapsed since load: {elapsed_ms:.1f} ms")
        return 0

    if elapsed_ms / 1000.0 > MAX_REAL_PATCH_ELAPSED_SECONDS:
        raise PatchAbort(
            f"discovery took {elapsed_ms:.1f} ms after load; refusing a late write "
            f"(limit {MAX_REAL_PATCH_ELAPSED_SECONDS:.1f} s)"
        )

    webman.write_memory(vsh_pid, address, REPLACEMENT)
    print("SETMEM request completed")
    after = webman.read_memory(vsh_pid, address, 16)
    if after[:4] != REPLACEMENT:
        raise PatchAbort(
            "write was not verified: expected 41 3D 21 04, got "
            f"{after[:4].hex(' ').upper()}"
        )
    if after[4:8] != before[4:8]:
        raise PatchAbort(
            "write was not verified: XPAD name pointer changed from "
            f"{before[4:8].hex(' ').upper()} to {after[4:8].hex(' ').upper()}"
        )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    print(f"after:  {after[:8].hex(' ').upper()}")
    print("PATCH VERIFIED")
    print(f"elapsed since load: {elapsed_ms:.1f} ms")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchAbort as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2)
