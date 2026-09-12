from __future__ import annotations

_TAG_TO_LABEL = {
    "Fahrzeug:ID4-auto": "id4",
    "Fahrzeug:Multivan-auto": "multivan",
}


def classify_vehicle(tag_names: list[str], content: str, vin_hints: dict[str, str]) -> str | None:
    label_to_vin = {label: vin for vin, label in vin_hints.items()}

    for tag_name in tag_names:
        label = _TAG_TO_LABEL.get(tag_name)
        if label is not None and label in label_to_vin:
            return label_to_vin[label]

    for vin in vin_hints:
        if vin in content:
            return vin

    return None
