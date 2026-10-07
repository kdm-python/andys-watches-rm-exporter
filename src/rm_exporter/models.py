from __future__ import annotations

from dataclasses import dataclass


ROYAL_MAIL_PACKAGE_FORMAT_IDENTIFIERS = {
    "undefined",
    "letter",
    "largeLetter",
    "smallParcel",
    "mediumParcel",
    "largeParcel",
    "parcel",
    "documents",
}

# --- Medusa ---


@dataclass
class MedusaItem:
    title: str
    variant_title: str | None
    sku: str | None
    quantity: int
    unit_price: float


@dataclass
class MedusaAddress:
    first_name: str
    last_name: str
    company: str | None
    address_1: str
    address_2: str | None
    city: str
    province: str | None
    postal_code: str
    country_code: str
    phone: str | None


@dataclass
class MedusaOrder:
    id: str
    display_id: int
    created_at: str
    email: str
    subtotal: float
    shipping_total: float
    total: float
    shipping_address: MedusaAddress
    fulfillment_status: str

    items: list[MedusaItem]


# --- Royal Mail ---


@dataclass
class RoyalMailAddress:
    full_name: str
    company_name: str | None
    address_line_1: str
    address_line_2: str | None
    city: str
    county: str | None
    postcode: str
    country_code: str


@dataclass
class RoyalMailRecipient:
    address: RoyalMailAddress
    phone_number: str | None
    email_address: str | None


@dataclass
class RoyalMailProduct:
    name: str
    quantity: int
    unit_value: float
    sku: str | None


@dataclass
class RoyalMailPackage:
    weight_in_grams: int
    package_format_identifier: str
    contents: list[RoyalMailProduct]

    def __post_init__(self):
        if not 1 <= self.weight_in_grams <= 30000:
            raise ValueError("weight_in_grams must be between 1 and 30000")
        if self.package_format_identifier not in ROYAL_MAIL_PACKAGE_FORMAT_IDENTIFIERS:
            raise ValueError(
                "package_format_identifier must be a Royal Mail package format"
            )


@dataclass
class RoyalMailOrder:
    order_reference: str
    order_date: str
    subtotal: float
    shipping_cost_charged: float
    total: float
    recipient: RoyalMailRecipient
    packages: list[RoyalMailPackage]
