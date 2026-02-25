"""sqladmin-based admin panel for the Bitrix imbot service.

Mounts at ``/admin`` and provides:
- Company info editor (single-row table)
- Read-only product catalog browser
- Read-only chat message viewer

Authentication uses HTTP Basic via a custom sqladmin ``AuthenticationBackend``
when ``ADMIN_PASSWORD`` is set in the environment; otherwise the panel is
open (suitable only for local/internal use).
"""
from __future__ import annotations

import secrets
from typing import Optional

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse
from wtforms import TextAreaField

from .config import Settings
from .models import ChatMessage, CompanyInfo, Product


# ---------------------------------------------------------------------------
# HTTP Basic authentication backend
# ---------------------------------------------------------------------------


class BasicAuthBackend(AuthenticationBackend):
    """Minimal session-cookie auth with a single admin user."""

    def __init__(self, secret_key: str, username: str, password: str) -> None:
        super().__init__(secret_key=secret_key)
        self._username = username
        self._password = password

    async def login(self, request: Request) -> bool:
        form = await request.form()
        user = str(form.get("username", ""))
        pwd = str(form.get("password", ""))
        if secrets.compare_digest(user, self._username) and secrets.compare_digest(pwd, self._password):
            request.session.update({"admin_authenticated": "1"})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> Optional[RedirectResponse]:
        if request.session.get("admin_authenticated") == "1":
            return None  # authenticated
        return RedirectResponse(request.url_for("admin:login"), status_code=302)


# ---------------------------------------------------------------------------
# Model views
# ---------------------------------------------------------------------------


class CompanyInfoAdmin(ModelView, model=CompanyInfo):
    name = "Информация о компании"
    name_plural = "Информация о компании"
    icon = "fa-solid fa-building"

    # Show these columns in the list view
    column_list = [
        CompanyInfo.company_name,
        CompanyInfo.phone,
        CompanyInfo.working_hours,
        CompanyInfo.updated_at,
    ]

    # All fields in the edit form
    form_columns = [
        "company_name",
        "address",
        "phone",
        "email",
        "website",
        "working_hours",
        "delivery_info",
        "payment_info",
        "extra_faq",
    ]

    # Override extra_faq to use a large textarea
    form_overrides = {
        "extra_faq": TextAreaField,
        "delivery_info": TextAreaField,
        "payment_info": TextAreaField,
    }

    form_args = {
        "extra_faq": {
            "label": "Дополнительная информация / FAQ",
            "render_kw": {"rows": 10},
        },
        "delivery_info": {
            "label": "Информация о доставке",
            "render_kw": {"rows": 4},
        },
        "payment_info": {
            "label": "Способы оплаты",
            "render_kw": {"rows": 3},
        },
    }

    # Prevent deleting the single row
    can_delete = False
    can_create = False

    # Keep the list and detail readable
    column_labels = {
        "company_name": "Компания",
        "address": "Адрес",
        "phone": "Телефон",
        "email": "E-mail",
        "website": "Сайт",
        "working_hours": "Часы работы",
        "delivery_info": "Доставка",
        "payment_info": "Оплата",
        "extra_faq": "Доп. FAQ",
        "updated_at": "Обновлено",
    }


class ProductAdmin(ModelView, model=Product):
    name = "Товар"
    name_plural = "Каталог товаров"
    icon = "fa-solid fa-fish"

    can_create = False
    can_edit = False
    can_delete = False

    column_list = [
        Product.title,
        Product.category,
        Product.price,
        Product.quantity,
        Product.sku,
        Product.updated_at,
    ]

    column_searchable_list = [Product.title, Product.category, Product.sku]
    column_sortable_list = [Product.title, Product.category, Product.price, Product.updated_at]
    column_default_sort = [(Product.category, False), (Product.title, False)]
    page_size = 50

    column_labels = {
        "title": "Название",
        "category": "Категория",
        "price": "Цена",
        "priceold": "Старая цена",
        "quantity": "Остаток",
        "sku": "Артикул",
        "url": "URL",
        "updated_at": "Обновлено",
    }


class ChatMessageAdmin(ModelView, model=ChatMessage):
    name = "Сообщение"
    name_plural = "История чатов"
    icon = "fa-solid fa-comments"

    can_create = False
    can_edit = False
    can_delete = True  # allow manual cleanup

    column_list = [
        ChatMessage.dialog_id,
        ChatMessage.role,
        ChatMessage.text,
        ChatMessage.created_at,
    ]

    column_searchable_list = [ChatMessage.dialog_id, ChatMessage.role]
    column_sortable_list = [ChatMessage.created_at, ChatMessage.dialog_id]
    column_default_sort = (ChatMessage.created_at, True)  # newest first
    page_size = 100

    column_labels = {
        "dialog_id": "Dialog ID",
        "role": "Роль",
        "text": "Текст",
        "created_at": "Время",
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_admin(app, engine, settings: Settings) -> Admin:
    """Create and configure the sqladmin ``Admin`` instance.

    ``engine`` must be the **sync** SQLAlchemy engine that sqladmin expects
    *or* an async engine — sqladmin ≥0.20 accepts both.
    """
    auth_backend: Optional[BasicAuthBackend] = None
    if settings.admin_password:
        auth_backend = BasicAuthBackend(
            secret_key=settings.admin_secret,
            username=settings.admin_username,
            password=settings.admin_password,
        )

    admin = Admin(
        app,
        engine,
        title="МояРыба — Панель управления",
        authentication_backend=auth_backend,
    )

    admin.add_view(CompanyInfoAdmin)
    admin.add_view(ProductAdmin)
    admin.add_view(ChatMessageAdmin)

    return admin
