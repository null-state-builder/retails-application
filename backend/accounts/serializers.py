from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from accounts.actor_policies import actions_for
from accounts.floors import describe_floors, floor_violations
from accounts.models import NAV_GROUPS, ActorPolicy, Role, User
from accounts.permissions import visible_sections
from accounts.role_lists import HEAD_OFFICE_VALUE_ACTORS
from accounts.sections import CAPABILITY_ORDER, is_valid_capability, is_valid_section
from accounts.till_pin import may_hold_till_pin
from approvals.models import ApprovalPolicy
from masters.models import Brand, Store
from masters.scoping import BRAND_SCOPE, scoped_brands, scoped_stores, visible_store_ids

_T = TypeVar("_T")


class RoleSerializer(serializers.ModelSerializer[Role]):
    class Meta:
        model = Role
        fields = [
            "id",
            "code",
            "name",
            "description",
            "landing_page",
            "nav_groups",
            "is_system",
            "is_active",
        ]


class AdminRoleSerializer(serializers.ModelSerializer[Role]):
    user_count = serializers.IntegerField(source="users.count", read_only=True)

    class Meta:
        model = Role
        fields = [
            "id",
            "code",
            "name",
            "description",
            "landing_page",
            "nav_groups",
            "section_access",
            "permissions_map",
            "is_system",
            "is_active",
            "user_count",
        ]
        read_only_fields = ["is_system", "user_count"]
        extra_kwargs = {"code": {"validators": [UniqueValidator(queryset=Role.objects.all())]}}

    def validate_nav_groups(self, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(NAV_GROUPS))
        if unknown:
            raise serializers.ValidationError(f"Unknown nav group(s): {', '.join(unknown)}")
        return value

    def validate_section_access(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise serializers.ValidationError("section_access must be an object.")
        unknown = sorted(k for k in value if not is_valid_section(k))
        if unknown:
            raise serializers.ValidationError(f"Unknown section(s): {', '.join(unknown)}")
        for section, entry in value.items():
            capability = entry.get("capability") if isinstance(entry, dict) else None
            if not is_valid_capability(capability or ""):
                raise serializers.ValidationError(
                    f"{section}: capability must be one of {', '.join(CAPABILITY_ORDER)}."
                )
        return value

    def validate_code(self, value: str) -> str:
        if self.instance and self.instance.is_system and value != self.instance.code:
            raise serializers.ValidationError("System role codes cannot be changed.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """The money floor, on the older whole-role door too (#173).

        The access-matrix editor is the screen people will use, but this
        endpoint edits the same ``section_access`` column and would otherwise be
        the way around it. A floor with one gate is not a floor.

        The **code** is checked as carefully as the access, because the floor
        keys on it: renaming a role that holds full Money to a code the floor
        binds would leave a row nobody could have written directly. So a rename
        re-tests whatever access that role will hold afterwards, sent or stored.
        """
        code = attrs.get("code") or getattr(self.instance, "code", "")
        access = attrs.get("section_access")
        if access is None:
            if "code" not in attrs or self.instance is None:
                return attrs
            access = self.instance.section_access or {}
        crossed = floor_violations(str(code), access)
        if crossed:
            raise serializers.ValidationError({"section_access": describe_floors(crossed)})
        return attrs


class ActorPolicySerializer(serializers.ModelSerializer[ActorPolicy]):
    class Meta:
        model = ActorPolicy
        fields = ["id", "action", "label", "description", "roles"]
        read_only_fields = ["action"]

    #: The two actions floor rule 3 owns. Policy may *narrow* who inwards a PT
    #: or executes a V-flip; it may never widen the pair, because the GL engine
    #: refuses anyone else anyway and a Setup screen must not promise otherwise.
    FLOOR_BOUND_ACTIONS = ("ptmapper.post_and_reverse_pt", "outbound.execute_vflip")

    def validate_roles(self, value: list[str]) -> list[str]:
        names = dict(Role.objects.values_list("code", "name"))
        unknown = sorted(set(value) - set(names))
        if unknown:
            raise serializers.ValidationError(f"Unknown role(s): {', '.join(unknown)}")
        outside_floor = sorted(set(value) - HEAD_OFFICE_VALUE_ACTORS)
        if getattr(self.instance, "action", "") in self.FLOOR_BOUND_ACTIONS and outside_floor:
            refused = ", ".join(names[code] for code in outside_floor)
            raise serializers.ValidationError(
                "Floor rule: PT inwarding and V-flip execution are limited to "
                f"Accounts or Owner. {refused} cannot be added."
            )
        return sorted(set(value))


class ApprovalPolicyAdminSerializer(serializers.ModelSerializer[ApprovalPolicy]):
    # DRF's Field base class also carries a `label` attribute (the form label),
    # typed `str | _StrPromise | None` in the stubs; declaring a same-named
    # serializer field is a normal DRF pattern the stubs don't model, so the
    # assignment looks like a type clash that isn't one at runtime.
    label = serializers.CharField(read_only=True)  # type: ignore[assignment]

    class Meta:
        model = ApprovalPolicy
        fields = [
            "id",
            "kind",
            "label",
            "tolerance_paise",
            "band_paise",
            "band_roles",
            "escalated_roles",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        known = set(Role.objects.values_list("code", flat=True))
        supplied = set(attrs.get("band_roles", [])) | set(attrs.get("escalated_roles", []))
        unknown = sorted(supplied - known)
        if unknown:
            raise serializers.ValidationError({"roles": f"Unknown role(s): {', '.join(unknown)}"})
        return attrs


class BrandMiniSerializer(serializers.Serializer[Brand]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class StoreMiniSerializer(serializers.Serializer[Store]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    store_type = serializers.CharField()
    state_name = serializers.CharField(source="gstin.state_name")
    state_code = serializers.CharField(source="gstin.state_code")
    gstin_number = serializers.CharField(source="gstin.gstin")


class UserProfileSerializer(serializers.ModelSerializer[User]):
    role = RoleSerializer(read_only=True)
    nav_groups = serializers.SerializerMethodField()
    landing_page = serializers.SerializerMethodField()
    scope_label = serializers.CharField(source="get_scope_type_display", read_only=True)
    entity_name = serializers.CharField(source="entity.name", read_only=True)
    stores = serializers.SerializerMethodField()
    # SIDEBAR RBAC contract (issue #85): the server, not the client, decides what
    # each person sees and may do. `sections` is the ordered, role-filtered
    # sidebar with a capability per section; `capabilities` is the flat
    # {section: capability} the client gates on; `business_units` is the units
    # the user may act in (fail-closed — empty ⇒ nothing).
    sections = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    actions = serializers.SerializerMethodField()
    business_units = serializers.SerializerMethodField()
    all_business_units = serializers.SerializerMethodField()
    # What the top-bar switcher offers this person (issue #88): a list of units,
    # or — for a brand manager, whose scope cuts across every store — the brands
    # they are assigned instead. The client renders the payload; it never infers
    # the mode or the list from the role code.
    business_unit_mode = serializers.SerializerMethodField()
    assigned_brands = serializers.SerializerMethodField()
    # Whether this person has a counter PIN, and never what it is (#182). The
    # card that offers to set one has to be able to say "you have one already",
    # and a hash is a credential that has no business in a profile response.
    has_till_pin = serializers.SerializerMethodField()
    may_hold_till_pin = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "full_name",
            "is_superuser",
            "role",
            "scope_type",
            "scope_label",
            "entity",
            "entity_name",
            "stores",
            "nav_groups",
            "landing_page",
            "sections",
            "capabilities",
            "actions",
            "business_units",
            "all_business_units",
            "business_unit_mode",
            "assigned_brands",
            "has_till_pin",
            "may_hold_till_pin",
        ]

    def get_has_till_pin(self, obj: User) -> bool:
        return bool(obj.till_pin_hash)

    def get_may_hold_till_pin(self, obj: User) -> bool:
        return may_hold_till_pin(obj)

    def get_nav_groups(self, obj: User) -> list[str]:
        if obj.is_superuser:
            return list(NAV_GROUPS)
        return list(obj.role.nav_groups) if obj.role else []

    def get_landing_page(self, obj: User) -> str:
        return obj.role.landing_page if obj.role else "home"

    def _cached(self, obj: User, key: str, compute: Callable[[], _T]) -> _T:
        """Memoize a derived value per (field, object) so the shared scope and
        section walks run once per response — this serializer renders four
        SerializerMethodFields off the same two computations."""
        store = self.__dict__.setdefault("_derived", {})
        cache_key = (key, id(obj))
        if cache_key not in store:
            store[cache_key] = compute()
        result: _T = store[cache_key]
        return result

    def get_stores(self, obj: User) -> list[dict[str, Any]]:
        return self._cached(
            obj, "stores", lambda: list(StoreMiniSerializer(scoped_stores(obj), many=True).data)
        )

    def get_sections(self, obj: User) -> list[dict[str, Any]]:
        return self._cached(obj, "sections", lambda: visible_sections(obj))

    def get_capabilities(self, obj: User) -> dict[str, str]:
        # Flat {section: capability} the client gates on — projected from the
        # already-computed sections list, not a second resolution pass.
        return {str(s["code"]): str(s["capability"]) for s in self.get_sections(obj)}

    def get_actions(self, obj: User) -> list[str]:
        # The stored actor policies this person satisfies (#75). The ladder above
        # cannot express them: two roles can sit on the same rung of a section
        # and still differ on one action inside it.
        return actions_for(obj)

    def get_business_units(self, obj: User) -> list[dict[str, Any]]:
        # The units the user may act in are exactly their scoped stores. `all`-
        # scoped users act network-wide, so the concrete list is every store
        # (with `all_business_units` flagged so the switcher can offer an "All
        # units" option instead of 17 rows).
        return self.get_stores(obj)

    def get_all_business_units(self, obj: User) -> bool:
        # A brand manager reaches every store, but a network *unit* view is not
        # what they work in — their "all" is all their brands.
        if obj.scope_type == BRAND_SCOPE and not obj.is_superuser:
            return False
        return self._cached(obj, "all_units", lambda: visible_store_ids(obj) is None)

    def get_business_unit_mode(self, obj: User) -> str:
        return "brands" if obj.scope_type == BRAND_SCOPE and not obj.is_superuser else "units"

    def get_assigned_brands(self, obj: User) -> list[dict[str, Any]]:
        if obj.scope_type != BRAND_SCOPE or obj.is_superuser:
            return []
        return list(BrandMiniSerializer(scoped_brands(obj), many=True).data)


class AdminUserSerializer(serializers.ModelSerializer[User]):
    role = RoleSerializer(read_only=True)
    role_id = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.filter(is_active=True),
        source="role",
        write_only=True,
        required=False,
        allow_null=True,
    )
    scope_label = serializers.CharField(source="get_scope_type_display", read_only=True)
    entity_name = serializers.CharField(source="entity.name", read_only=True)
    stores = StoreMiniSerializer(many=True, read_only=True)
    store_ids = serializers.PrimaryKeyRelatedField(
        queryset=Store.objects.filter(is_active=True),
        source="stores",
        write_only=True,
        many=True,
        required=False,
    )
    brands = BrandMiniSerializer(many=True, read_only=True)
    brand_ids = serializers.PrimaryKeyRelatedField(
        queryset=Brand.objects.filter(is_active=True),
        source="brands",
        write_only=True,
        many=True,
        required=False,
    )
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "full_name",
            "role",
            "role_id",
            "scope_type",
            "scope_label",
            "entity",
            "entity_name",
            "stores",
            "store_ids",
            "brands",
            "brand_ids",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "password",
        ]
        read_only_fields = ["is_superuser", "date_joined"]

    def validate_password(self, value: str) -> str:
        if value:
            validate_password(value)
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        scope = attrs.get("scope_type") or getattr(self.instance, "scope_type", "store")
        stores = attrs.get("stores")
        if scope == "store" and stores is not None and len(stores) == 0:
            raise serializers.ValidationError(
                {"store_ids": "A store-scoped user needs at least one store."}
            )
        brands = attrs.get("brands")
        if scope == BRAND_SCOPE and brands is not None and len(brands) == 0:
            raise serializers.ValidationError(
                {"brand_ids": "A brand-scoped user needs at least one brand."}
            )
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Password is required for a new user."})
        return attrs

    def create(self, validated_data: dict[str, Any]) -> User:
        stores = validated_data.pop("stores", [])
        brands = validated_data.pop("brands", [])
        password = validated_data.pop("password", "")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        user.stores.set(stores)
        user.brands.set(brands)
        return user

    def update(self, instance: User, validated_data: dict[str, Any]) -> User:
        stores = validated_data.pop("stores", None)
        brands = validated_data.pop("brands", None)
        password = validated_data.pop("password", "")
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        if stores is not None:
            instance.stores.set(stores)
        if brands is not None:
            instance.brands.set(brands)
        return instance
