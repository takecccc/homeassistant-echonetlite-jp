from __future__ import annotations

from typing import Any


class Eoj0287Handler:
    """Class-driven behavior for EOJ 0x0287 (power distribution board metering)."""

    CLASS_CODE = "0287"

    async def augment_channels(
        self,
        client: Any,
        host: str,
        eoj_gc: int,
        eoj_cc: int,
        eoj_ci: int,
        get_map: list[int],
        set_map: list[int],
        payload: dict[str, Any],
    ) -> list[str]:
        # Reuse client's low-level methods to keep test compatibility.
        if eoj_gc != 0x02 or eoj_cc != 0x87:
            return []
        b1 = payload.get("0xB1")
        if b1 is None:
            b1 = await client._get_single_epc_value(host, eoj_gc, eoj_cc, eoj_ci, 0xB1)
            if b1 is not None:
                payload["0xB1"] = b1
        b8 = payload.get("0xB8")
        if b8 is None:
            b8 = await client._get_single_epc_value(host, eoj_gc, eoj_cc, eoj_ci, 0xB8)
            if b8 is not None:
                payload["0xB8"] = b8

        count_simplex = client._decode_channel_count(b1)
        count_duplex = client._decode_channel_count(b8)
        count_both_known = isinstance(count_simplex, int) and isinstance(count_duplex, int)
        has_count_info = isinstance(count_simplex, int) or isinstance(count_duplex, int)
        count = 0
        if isinstance(count_simplex, int):
            count += count_simplex
        if isinstance(count_duplex, int):
            count += count_duplex
        if count <= 0:
            count = 41
        max_channel = min(count, 41)
        start_channel = 1
        fetch_range = max(1, max_channel - start_channel + 1)

        energy_by_ch = await client._obtain_0287_simplex_list(
            host,
            eoj_gc,
            eoj_cc,
            eoj_ci,
            range_epc=0xB2,
            list_epc=0xB3,
            start_channel=start_channel,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xB2 in set_map),
            ignore_reported_start=True,
            cached_value=payload.get("0xB3"),
        )
        power_by_ch = await client._obtain_0287_simplex_list(
            host,
            eoj_gc,
            eoj_cc,
            eoj_ci,
            range_epc=0xB6,
            list_epc=0xB7,
            start_channel=start_channel,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xB6 in set_map),
            ignore_reported_start=True,
            cached_value=payload.get("0xB7"),
        )
        duplex_energy_by_ch = await client._obtain_0287_duplex_energy_list(
            host,
            eoj_gc,
            eoj_cc,
            eoj_ci,
            range_epc=0xB9,
            list_epc=0xBA,
            start_channel=start_channel,
            fetch_range=fetch_range,
            can_set_range=(0xB9 in set_map),
            ignore_reported_start=True,
            cached_value=payload.get("0xBA"),
        )
        duplex_power_by_ch = await client._obtain_0287_simplex_list(
            host,
            eoj_gc,
            eoj_cc,
            eoj_ci,
            range_epc=0xBD,
            list_epc=0xBE,
            start_channel=start_channel,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xBD in set_map),
            ignore_reported_start=True,
            cached_value=payload.get("0xBE"),
        )
        energy_by_ch = client._limit_0287_list_items(energy_by_ch, count_simplex)
        power_by_ch = client._limit_0287_list_items(power_by_ch, count_simplex)
        duplex_energy_by_ch = client._limit_0287_list_items(duplex_energy_by_ch, count_duplex)
        duplex_power_by_ch = client._limit_0287_list_items(duplex_power_by_ch, count_duplex)
        energy_by_ch = client._merge_0287_channel_values(
            simplex_by_ch=energy_by_ch,
            duplex_by_ch=duplex_energy_by_ch,
            count_simplex=count_simplex,
            count_duplex=count_duplex,
        )
        power_by_ch = client._merge_0287_channel_values(
            simplex_by_ch=power_by_ch,
            duplex_by_ch=duplex_power_by_ch,
            count_simplex=count_simplex,
            count_duplex=count_duplex,
        )

        detected_max = max(max(energy_by_ch.keys(), default=0), max(power_by_ch.keys(), default=0))
        if detected_max < start_channel:
            if has_count_info and count > 0:
                detected_max = max_channel
            else:
                errors = payload.get("_errors")
                if not isinstance(errors, list):
                    errors = []
                    payload["_errors"] = errors
                errors.append("0287:list_data_empty")
                return []
        if not count_both_known and detected_max > max_channel:
            max_channel = min(detected_max, 41)

        virtual_get_map: list[str] = []
        for ch in range(start_channel, max_channel + 1):
            energy = energy_by_ch.get(ch, "FFFFFFFE")
            power = power_by_ch.get(ch, "7FFFFFFE")
            if len(energy) != 8:
                energy = "FFFFFFFE"
            if len(power) != 8:
                power = "7FFFFFFE"
            composite = f"{energy}{power}"
            if 1 <= ch <= 32:
                epc = 0xD0 + (ch - 1)
                payload[client._epc_to_key(epc)] = composite
                continue
            key = client._virtual_0287_key(ch)
            payload[key] = composite
            virtual_get_map.append(key)
        return virtual_get_map

    def build_channel_meta(self, channel: int) -> dict[str, Any]:
        return {
            "name": f"計測チャンネル{channel}",
            "description": "積算電力量(kWh)と瞬時電力(W)を並べて示す",
            "short_name": f"measurementChannel{channel}",
            "type": "object",
            "format": "",
            "unit": None,
            "multiple": None,
            "minimum": None,
            "maximum": None,
            "base": None,
            "enum": {},
            "no_data_codes": [],
            "refs": [],
            "object_fields": [
                {
                    "key": "electricEnergy",
                    "name": "積算電力量計測値",
                    "type": "number",
                    "format": "uint32",
                    "size": 4,
                    "unit": "kWh",
                    "multiple": None,
                    "coefficient": ["0xC2"],
                    "enum": {},
                    "no_data_codes": [0xFFFFFFFE],
                },
                {
                    "key": "instantaneousPower",
                    "name": "瞬時電力計測値",
                    "type": "number",
                    "format": "int32",
                    "size": 4,
                    "unit": "W",
                    "multiple": None,
                    "coefficient": [],
                    "enum": {},
                    "no_data_codes": [0x7FFFFFFE],
                },
            ],
        }

    def resolve_epc_metadata(self, client: Any, epc_key: str) -> dict[str, Any] | None:
        try:
            epc = client._epc_from_key(epc_key)
        except ValueError:
            channel = client._virtual_0287_channel(epc_key)
            if channel is None:
                return None
            return self.build_channel_meta(channel)
        if 0xD0 <= epc <= 0xEF:
            channel = (epc - 0xD0) + 1
            return self.build_channel_meta(channel)
        return None

    def build_sensor_keys(self, client: Any, data: dict[str, Any]) -> list[str]:
        payload = data.get("payload", {})
        payload_map = payload if isinstance(payload, dict) else {}
        keys = set(client._build_sensor_keys_default(data))

        b1 = client._decode_channel_count(payload_map.get("0xB1"))
        b8 = client._decode_channel_count(payload_map.get("0xB8"))
        total = 0
        if isinstance(b1, int):
            total += b1
        if isinstance(b8, int):
            total += b8

        if total > 0:
            max_channel = min(total, 41)
            for ch in range(1, min(max_channel, 32) + 1):
                keys.add(client._epc_to_key(0xD0 + (ch - 1)))
            for ch in range(33, max_channel + 1):
                keys.add(client._virtual_0287_key(ch))
        else:
            for key in payload_map.keys():
                normalized = client._normalize_property_key(key)
                if normalized:
                    keys.add(normalized)

        return sorted(keys, key=client._sensor_key_sort)

    def build_fetch_map(self, get_map: list[int]) -> list[int]:
        return [epc for epc in get_map if not (0xD0 <= epc <= 0xEF)]
