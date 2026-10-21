"""
tests/test_barcode.py
---------------------
Unit tests for the barcode_service module.
Tests pure-python decoding of EAN-13, EAN-8 barcodes, checksum calculations,
and error handling.
"""
import io
import pytest
from PIL import Image

from src.services.barcode_service import (
    decode_barcode_from_image,
    create_ean13_test_image,
    calculate_ean13_checksum,
    decode_ean13_from_runs,
    decode_ean8_from_runs,
    _extract_runs_from_row,
    _normalize_to_n_modules,
)


class TestBarcodeServiceChecksum:
    def test_calculate_ean13_checksum_standard(self):
        # 800000000001 -> check digit 9
        assert calculate_ean13_checksum("800000000001") == 9

    def test_calculate_ean13_checksum_various(self):
        # 590123412345 -> check digit 7
        assert calculate_ean13_checksum("590123412345") == 7
        # 400638133393 -> check digit 1
        assert calculate_ean13_checksum("400638133393") == 1


class TestBarcodeServiceDecode:
    def test_decode_valid_ean13_image(self):
        code = "8000000000019"
        img_bytes = create_ean13_test_image(code)
        result = decode_barcode_from_image(img_bytes)
        assert result is not None
        assert result["barcode"] == code
        assert result["format"] == "EAN-13"

    def test_decode_rotated_90_degrees(self):
        # Vertical orientation
        code = "8000000000019"
        img_bytes = create_ean13_test_image(code)
        img = Image.open(io.BytesIO(img_bytes)).rotate(90, expand=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        
        result = decode_barcode_from_image(buf.getvalue())
        assert result is not None
        assert result["barcode"] == code

    def test_decode_blank_image_returns_none(self):
        img = Image.new("RGB", (300, 150), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = decode_barcode_from_image(buf.getvalue())
        assert result is None

    def test_decode_corrupted_image_bytes(self):
        result = decode_barcode_from_image(b"not an image data")
        assert result is None

    def test_normalize_to_n_modules(self):
        # Test 4 bar widths normalizing to 7 modules
        widths = [10.0, 20.0, 10.0, 30.0]  # total 70 -> [1, 2, 1, 3]
        mods = _normalize_to_n_modules(widths, 7)
        assert mods == [1, 2, 1, 3]

    def test_normalize_to_n_modules_empty(self):
        assert _normalize_to_n_modules([0.0, 0.0, 0.0, 0.0], 7) is None

    def test_extract_runs(self):
        # Row with 5 white, 5 black, 5 white
        row = [255] * 5 + [0] * 5 + [255] * 5
        runs = _extract_runs_from_row(row, threshold=128)
        assert len(runs) == 3
        assert runs[0] == (False, 5.0)  # White
        assert runs[1] == (True, 5.0)   # Black
        assert runs[2] == (False, 5.0)  # White

    def test_decode_ean8_short_runs(self):
        assert decode_ean8_from_runs([(True, 1.0)] * 10) is None

    def test_decode_ean13_short_runs(self):
        assert decode_ean13_from_runs([(True, 1.0)] * 10) is None
