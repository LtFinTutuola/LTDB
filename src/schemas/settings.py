"""
src/schemas/settings.py
-----------------------
Pydantic schemas for the Settings management endpoints.
"""
from pydantic import BaseModel, Field


class SettingsResponse(BaseModel):
    db_connection_string: str
    excel_file_path: str
    excel_code_column: str
    api_key: str
    codes_deduction_agent_model: str


class SettingsUpdateRequest(BaseModel):
    db_connection_string: str = Field(..., min_length=1, description="Database connection string")
    excel_file_path: str = Field(..., min_length=1, description="Sales Excel file path")
    excel_code_column: str = Field(..., min_length=1, description="Barcode column letter in Excel")
    api_key: str = Field(..., min_length=1, description="Gemini API Key / Password")
    codes_deduction_agent_model: str = Field(..., min_length=1, description="Gemini model name")
