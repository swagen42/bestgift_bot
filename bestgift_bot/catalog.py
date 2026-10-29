from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


@dataclass(frozen=True)
class Variant:
    key: str
    title: str
    price: int


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    variants: dict[str, Variant]


@dataclass(frozen=True)
class Catalog:
    products: dict[str, Product]

    @classmethod
    def load(cls, path: Path) -> Catalog:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        raw_products = data.get("products")
        if not isinstance(raw_products, dict) or not raw_products:
            raise ValueError("Каталог должен содержать непустую таблицу products.")
        products = {}
        for key, item in raw_products.items():
            if not isinstance(item, dict):
                raise ValueError(f"Товар {key} должен быть таблицей TOML.")
            cls._validate_key(key)
            cls._validate_title(item.get("title"))
            raw_variants = item.get("variants")
            if not isinstance(raw_variants, dict) or not raw_variants:
                raise ValueError(f"У товара {key} нет таблицы вариантов variants.")
            variants = {}
            for variant_key, raw in raw_variants.items():
                if not isinstance(raw, dict):
                    raise ValueError(f"Вариант {key}/{variant_key} должен быть таблицей TOML.")
                cls._validate_key(variant_key)
                cls._validate_title(raw.get("title"))
                price = raw.get("price")
                if type(price) is not int or not 0 < price <= 10_000_000:
                    raise ValueError(f"Некорректная цена: {key}/{variant_key}")
                variants[variant_key] = Variant(variant_key, raw["title"], price)
            if not variants:
                raise ValueError(f"У товара {key} нет вариантов.")
            products[key] = Product(key, item["title"], variants)
        if not products:
            raise ValueError("Каталог пуст.")
        return cls(products)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,16}", key):
            raise ValueError("Ключи каталога: 1–16 символов a-z, 0-9 или _.")

    @staticmethod
    def _validate_title(title: str | None) -> None:
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError("Название в каталоге должно содержать от 1 до 100 символов.")

    def price_list(self) -> str:
        lines = ["Прайс", ""]
        for product in self.products.values():
            lines.append(product.title)
            lines.extend(f"• {v.title} — {money(v.price)}" for v in product.variants.values())
            lines.append("")
        lines.append("Оплату, доставку и детали заказа вы обсудите с мастером лично.")
        return "\n".join(lines)
