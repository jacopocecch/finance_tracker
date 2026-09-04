import pytest

from parsers import _strip_fineco_city, parse_transaction


def _fineco_card(before: str) -> dict:
    return {
        "remittance_information": [
            f"{before} Carta N. ***** 123 Data operazione 12/03/26"
        ],
    }


@pytest.mark.parametrize(
    "before,merchant",
    [
        # Previously kept the city: mixed-case city / digit-leading city / 2-token city
        ("NETFLIX.COM Milan IT", "NETFLIX.COM"),
        ("Netflix.com Los Gatos IT", "Netflix.com"),
        ("CMN E-BILLETS 234375 75PARIS FR", "CMN E-BILLETS 234375"),
        ("NEU*DIAL SERVICES Seraing BE", "NEU*DIAL SERVICES"),
        # Paris arrondissement suffix
        ("MOLESKINE 440948 75PARIS 1 FR", "MOLESKINE 440948"),
        ("LE BRELAN 403673 75PARIS 03 FR", "LE BRELAN 403673"),
        ("PISA ITALIA Pisa IT", "PISA ITALIA"),
        ("The Space Cinema 1 S.p Rome IT", "The Space Cinema 1 S.p"),
    ],
)
def test_fineco_strips_city_case_insensitively(before, merchant):
    assert _strip_fineco_city(before) == merchant
    assert parse_transaction(_fineco_card(before), "FinecoBank")["merchant"] == merchant


@pytest.mark.parametrize(
    "before,merchant",
    [
        # Prod-style merchants that were already stripped correctly: unchanged
        ("STARBUCKS PARIS FR", "STARBUCKS"),
        ("BEACH SPACE PISA IT", "BEACH SPACE"),
        ("IPER CASCINA CASCINA IT", "IPER CASCINA"),
        ("PONTEDERA 3 PONTEDERA IT", "PONTEDERA 3"),
        ("NON SOLO FUMETTO 3 PONTEDERA IT", "NON SOLO FUMETTO 3"),
        ("PIZZERIA PEPE & SALE PONTEDERA IT", "PIZZERIA PEPE & SALE"),
        ("MC DONALD S NAVACCHIO NAVACCHIO IT", "MC DONALD S NAVACCHIO"),
        ("WWW.AMAZON.* VZ4U391T5 LUXEMBOURG LU", "WWW.AMAZON.* VZ4U391T5"),
        ("TIGOTA' VIA DELL'OLMO. PONTEDERA IT", "TIGOTA' VIA DELL'OLMO."),
        ("PHARMACIE SARRET290346 PARIS FR", "PHARMACIE SARRET290346"),
        # All-caps 2-token cities keep the legacy 1-token strip (merchant
        # mappings in prod depend on it)
        ("PASTICCERIA BENVENUTI SAN MINIATO IT", "PASTICCERIA BENVENUTI SAN"),
        ("MARE FUORI ROSIGNANO MAR IT", "MARE FUORI ROSIGNANO"),
    ],
)
def test_fineco_existing_merchants_unchanged(before, merchant):
    assert _strip_fineco_city(before) == merchant


@pytest.mark.parametrize(
    "before",
    [
        # Trailing token is not city-like (dots / slashes / stars): leave as is
        "Amazon.it*NV1LE8OC4 www.amazon.it LU",
        "AMZN Mktp IT*9628J49W5 AMZN.COM/BILL LU",
        "VUELING AAJ947J VUELING.COM IT",
        # Country code must be uppercase
        "NETFLIX.COM Milan it",
        # Nothing to strip
        "BEACH SPACE",
        # A Title-case token that is not a known city prefix is not eaten
        # ("Starbucks" is the merchant, "Gosselies" the city)
    ],
)
def test_fineco_leaves_non_city_tails(before):
    assert _strip_fineco_city(before) == before


def test_fineco_two_token_city_only_for_known_prefixes():
    assert _strip_fineco_city("Ur 802210 Starbucks Gosselies BE") == "Ur 802210 Starbucks"
    assert _strip_fineco_city("The Space Livorno Livorno IT") == "The Space Livorno"
    assert _strip_fineco_city("Trattoria Rossi San Miniato IT") == "Trattoria Rossi"


def test_fineco_never_returns_empty_merchant():
    assert _strip_fineco_city("PARIS FR") == "PARIS FR"
    assert parse_transaction(_fineco_card("PARIS FR"), "FinecoBank")["merchant"] == "PARIS FR"


def test_fineco_date_and_description_preserved():
    raw = _fineco_card("NETFLIX.COM Milan IT")
    parsed = parse_transaction(raw, "FinecoBank")
    assert parsed["description"] == raw["remittance_information"][0]
    assert parsed["date"].isoformat() == "2026-03-12"
