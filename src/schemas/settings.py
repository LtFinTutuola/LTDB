"""
src/schemas/settings.py
-----------------------
Pydantic schemas for the Settings management endpoints (System/AI Admin settings
and Cash Register Excel settings shared with 'cassa' operators).
"""
import re
from typing import Optional
from pydantic import BaseModel, Field, model_validator

_EXCEL_COL_RE = re.compile(r"^[A-Za-z]{1,2}$")


def normalize_excel_column_letter(value: str, field_label: str) -> str:
    cleaned = (value or "").strip().upper()
    if not _EXCEL_COL_RE.match(cleaned):
        raise ValueError(
            f"Il campo '{field_label}' deve essere una lettera di colonna Excel valida (es. N, O, P, Q)."
        )
    return cleaned


class SettingsResponse(BaseModel):
    db_connection_string: str
    excel_file_path: str
    excel_code_column: str
    excel_phone_column: str = "O"
    excel_status_column: str = "P"
    excel_otp_column: str = "Q"
    api_key: str
    codes_deduction_agent_model: str


class SettingsUpdateRequest(BaseModel):
    db_connection_string: str = Field(..., min_length=1, description="Database connection string")
    excel_file_path: Optional[str] = Field(None, min_length=1, description="Sales Excel file path")
    excel_code_column: Optional[str] = Field(None, min_length=1, description="Barcode column letter in Excel")
    excel_phone_column: Optional[str] = Field(None, min_length=1, description="Customer phone column letter in Excel")
    excel_status_column: Optional[str] = Field(None, min_length=1, description="Customer info/status column letter in Excel")
    excel_otp_column: Optional[str] = Field(None, min_length=1, description="Discount OTP column letter in Excel")
    api_key: str = Field(..., min_length=1, description="Gemini API Key / Password")
    codes_deduction_agent_model: str = Field(..., min_length=1, description="Gemini model name")


class ExcelSettingsResponse(BaseModel):
    excel_file_path: str
    excel_code_column: str
    excel_phone_column: str
    excel_status_column: str
    excel_otp_column: str


class ExcelSettingsUpdateRequest(BaseModel):
    excel_file_path: str = Field(..., min_length=1, description="Sales Excel file path")
    excel_code_column: str = Field(..., min_length=1, description="Article/barcode column letter in Excel")
    excel_phone_column: str = Field(..., min_length=1, description="Customer phone column letter in Excel")
    excel_status_column: str = Field(..., min_length=1, description="Customer info/status column letter in Excel")
    excel_otp_column: str = Field(..., min_length=1, description="Discount OTP column letter in Excel")

    @model_validator(mode="after")
    def validate_columns(self) -> "ExcelSettingsUpdateRequest":
        self.excel_file_path = self.excel_file_path.strip()
        if not self.excel_file_path:
            raise ValueError("Il percorso del file Excel non può essere vuoto.")

        self.excel_code_column = normalize_excel_column_letter(
            self.excel_code_column, "Colonna codice articolo"
        )
        self.excel_phone_column = normalize_excel_column_letter(
            self.excel_phone_column, "Colonna numero di telefono"
        )
        self.excel_status_column = normalize_excel_column_letter(
            self.excel_status_column, "Colonna info cliente"
        )
        self.excel_otp_column = normalize_excel_column_letter(
            self.excel_otp_column, "Colonna codici sconto"
        )

        cols = [
            self.excel_code_column,
            self.excel_phone_column,
            self.excel_status_column,
            self.excel_otp_column,
        ]
        if len(set(cols)) != len(cols):
            raise ValueError(
                "Le colonne per codice articolo, telefono, info cliente e codici sconto devono essere tutte distinte tra loro."
            )
        return self
