"""Bounded public registration input; secrets are write-only, never summary data."""

from __future__ import annotations

from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework import serializers

from accounts.models import User
from accounts.role_assignments import INITIAL_ROLE_CODES


class StrictInput(serializers.Serializer[Any]):
    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError("The request contains unknown fields.")
        return cast(dict[str, Any], super().to_internal_value(data))


class InitialPersonInput(StrictInput):
    name = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=120)
    staff_code = serializers.SlugField(max_length=40)
    temporary_password = serializers.CharField(
        max_length=128, write_only=True, trim_whitespace=False
    )

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        data["email"] = data["email"].lower()
        candidate = User(
            email=data["email"], username=data["email"][:60], full_name=data["name"]
        )
        try:
            validate_password(data["temporary_password"], candidate)
        except ValidationError as exc:
            raise serializers.ValidationError(
                {"temporary_password": exc.messages}
            ) from None
        return data


class ProposedPersonInput(StrictInput):
    name = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=120)
    staff_code = serializers.SlugField(max_length=40)
    role_code = serializers.ChoiceField(choices=sorted(INITIAL_ROLE_CODES))

    def validate_email(self, value: str) -> str:
        return value.lower()


class CompanyInput(StrictInput):
    code = serializers.SlugField(max_length=24)
    name = serializers.CharField(max_length=160)
    legal_name = serializers.CharField(max_length=160)
    pan = serializers.RegexField(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    gstin = serializers.RegexField(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
    state_code = serializers.RegexField(r"^[0-9]{2}$")
    state_name = serializers.CharField(max_length=40)
    billing_address = serializers.CharField(max_length=1000)
    timezone = serializers.CharField(max_length=60, default="Asia/Kolkata")
    currency = serializers.RegexField(r"^[A-Z]{3}$", default="INR")
    locale = serializers.CharField(max_length=20, default="en-IN")
    country = serializers.RegexField(r"^[A-Z]{2}$", default="IN")

    def validate_timezone(self, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Choose a recognised timezone.") from None
        return value

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        if (
            data["gstin"][:2] != data["state_code"]
            or data["gstin"][2:12] != data["pan"]
        ):
            raise serializers.ValidationError(
                "GSTIN must match the supplied PAN and state code."
            )
        # The retained GST workflows currently support India/INR. Editable
        # defaults are validated honestly instead of enabling unsupported tax.
        if data["country"] != "IN" or data["currency"] != "INR":
            raise serializers.ValidationError("This alpha supports India and INR.")
        return data


class FirstStoreInput(StrictInput):
    code = serializers.SlugField(max_length=16)
    name = serializers.CharField(max_length=120)
    city = serializers.CharField(max_length=80)
    address = serializers.CharField(max_length=1000)
    setup_kind = serializers.ChoiceField(choices=["new", "existing"])
    source_system = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    counter_count = serializers.IntegerField(min_value=1, max_value=1, default=1)

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        if data["setup_kind"] == "existing" and not data["source_system"]:
            raise serializers.ValidationError(
                {"source_system": "Name the software supplying the opening stock."}
            )
        return data


class RegistrationInput(StrictInput):
    command_id = serializers.UUIDField()
    company = CompanyInput()
    store = FirstStoreInput()
    owner = InitialPersonInput()
    admin = InitialPersonInput()
    proposed_team: serializers.ListSerializer[Any] = serializers.ListSerializer(
        child=ProposedPersonInput(), required=False, default=list, max_length=50
    )

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        people = [data["owner"], data["admin"], *data["proposed_team"]]
        for key in ("email", "staff_code"):
            values = [str(person[key]).casefold() for person in people]
            if len(values) != len(set(values)):
                raise serializers.ValidationError(
                    f"Every initial person needs a separate {key}."
                )
        if data["owner"]["temporary_password"] == data["admin"]["temporary_password"]:
            raise serializers.ValidationError(
                "Owner and Admin must use separate temporary passwords."
            )
        return data


class ConfirmationInput(StrictInput):
    email = serializers.EmailField(max_length=120)
    temporary_password = serializers.CharField(
        max_length=128, write_only=True, trim_whitespace=False
    )
    summary_hash = serializers.RegexField(r"^[a-f0-9]{64}$", required=False)
    acknowledged = serializers.BooleanField(required=False)

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("summary_hash") and data.get("acknowledged") is not True:
            raise serializers.ValidationError(
                "Personally acknowledge the exact initial summary before confirming."
            )
        return data


class RegistrationEditInput(RegistrationInput):
    current_owner_password = serializers.CharField(
        max_length=128, write_only=True, trim_whitespace=False
    )
