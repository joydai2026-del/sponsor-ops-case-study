"""Slot capacity: the booking-conflict checker."""

from .capacity import (
    Product,
    Placement,
    CapacityLedger,
    Override,
    ConflictError,
    NotBookable,
    BookingRefused,
    UnknownProduct,
    scope_key_for,
)

__all__ = [
    "Product",
    "Placement",
    "CapacityLedger",
    "Override",
    "ConflictError",
    "NotBookable",
    "BookingRefused",
    "UnknownProduct",
    "scope_key_for",
]
