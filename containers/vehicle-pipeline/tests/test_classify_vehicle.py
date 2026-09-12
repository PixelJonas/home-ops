from vehicle_pipeline.classify_vehicle import classify_vehicle

VEHICLES = {"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}


def test_classifies_by_id4_tag() -> None:
    assert classify_vehicle(["Fahrzeug:ID4-auto"], "irrelevant content", VEHICLES) == "WVGZZZE27SE017858"


def test_classifies_by_multivan_tag() -> None:
    assert classify_vehicle(["Fahrzeug:Multivan-auto"], "irrelevant", VEHICLES) == "WV2ZZZ7HZNH000000"


def test_falls_back_to_vin_in_content_when_no_tag() -> None:
    content = "Fahrzeugschein VIN: WVGZZZE27SE017858 ausgestellt am ..."
    assert classify_vehicle([], content, VEHICLES) == "WVGZZZE27SE017858"


def test_returns_none_when_no_signal() -> None:
    assert classify_vehicle(["Sonstiges"], "no vin here", VEHICLES) is None


def test_tag_takes_precedence_over_conflicting_content_vin() -> None:
    content = "mentions WV2ZZZ7HZNH000000 somewhere"
    assert classify_vehicle(["Fahrzeug:ID4-auto"], content, VEHICLES) == "WVGZZZE27SE017858"
