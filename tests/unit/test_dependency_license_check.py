from scripts.check_dependency_licenses import PROHIBITED_LICENSE, validate_licenses


def test_validate_licenses_accepts_reviewed_permissive_and_file_level_licenses() -> None:
    assert (
        validate_licenses(
            "example",
            {
                "apache": "Apache-2.0",
                "mit": "MIT",
                "mpl": "MPL-2.0",
            },
        )
        == []
    )


def test_validate_licenses_rejects_missing_and_prohibited_licenses() -> None:
    errors = validate_licenses(
        "example",
        {
            "missing": "",
            "strong-copyleft": "GPL-3.0-only",
            "strong-copyleft-long-name": "GNU General Public License v2",
            "source-available": "BUSL-1.1",
        },
    )

    assert errors == [
        "example: missing has no license declaration",
        "example: source-available uses a prohibited license: BUSL-1.1",
        "example: strong-copyleft uses a prohibited license: GPL-3.0-only",
        "example: strong-copyleft-long-name uses a prohibited license: "
        "GNU General Public License v2",
    ]


def test_prohibited_pattern_does_not_confuse_lgpl_with_gpl() -> None:
    assert PROHIBITED_LICENSE.search("LGPL-3.0-only") is None
