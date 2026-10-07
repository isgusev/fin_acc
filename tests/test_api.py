"""HTTP-уровень: формат ошибок, аутентификация, CSRF, разграничение доступа, полный сценарий."""

import uuid
from decimal import Decimal

import pytest

from tests.conftest import API, PASSWORD, anon_csrf, login

D = Decimal


def assert_error(r, status, code, field=None):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "field"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert body["error"]["field"] == field
    return body["error"]


def type_id(client, code):
    refs = client.get(f"{API}/references").json()
    return next(t["id"] for t in refs["account_types"] if t["code"] == code)


def kind_id(client, name):
    refs = client.get(f"{API}/references").json()
    return next(k["id"] for k in refs["income_kinds"] if k["name"] == name)


def create_account(client, code, name):
    r = client.post(
        f"{API}/accounts", json={"name": name, "account_type_id": type_id(client, code)}
    )
    assert r.status_code == 201, r.text
    return r.json()


def create_income_plan(client, amount="1000", on="2026-01-10"):
    r = client.post(
        f"{API}/planning",
        json={
            "operation_type": "income",
            "planned_date": on,
            "amount_planned": amount,
            "income_kind_id": kind_id(client, "Иное"),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- формат ошибок


def test_validation_error_format(user_client):
    acc = create_account(user_client, "current", "Карта")
    r = user_client.post(
        f"{API}/operations",
        json={
            "operation_type": "expense",
            "account_id": acc["id"],
            "op_date": "2026-01-10",
            "amount": "0",
            "name": "x",
        },
    )
    err = assert_error(r, 422, "VALIDATION_ERROR", "amount")
    assert "0" in err["message"]


def test_validation_error_from_service(user_client):
    acc = create_account(user_client, "current", "Карта")
    r = user_client.post(
        f"{API}/operations",
        json={
            "operation_type": "expense",
            "account_id": acc["id"],
            "op_date": "2026-01-10",
            "amount": "10",
        },
    )
    assert_error(r, 422, "VALIDATION_ERROR", "name")


def test_validation_missing_field_and_extra_field(user_client):
    r = user_client.post(f"{API}/accounts", json={"account_type_id": 1})
    assert_error(r, 422, "VALIDATION_ERROR", "name")
    r = user_client.post(f"{API}/accounts", json={"name": "x", "account_type_id": 1, "owner_id": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_json_body(user_client):
    r = user_client.post(
        f"{API}/accounts", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_not_found_format(user_client):
    assert_error(user_client.get(f"{API}/accounts/{uuid.uuid4()}"), 404, "NOT_FOUND")
    assert_error(user_client.get(f"{API}/no-such-endpoint"), 404, "NOT_FOUND")


def test_conflict_format(user_client):
    create_account(user_client, "current", "Карта")
    r = user_client.post(
        f"{API}/accounts",
        json={"name": "Карта 2", "account_type_id": type_id(user_client, "current")},
    )
    assert_error(r, 409, "CONFLICT", "account_type_id")


def test_unauthorized_without_session(client):
    for path in ("/accounts", "/operations", "/planning", "/summary/balances", "/auth/me"):
        assert_error(client.get(f"{API}{path}"), 401, "UNAUTHORIZED")


def test_bogus_session_cookie_unauthorized(client):
    client.cookies.set("fa_session", "garbage")
    assert_error(client.get(f"{API}/accounts"), 401, "UNAUTHORIZED")


def test_csrf_missing_or_wrong(user_client):
    good = user_client.headers.pop("X-CSRF-Token")
    body = {"name": "x", "account_type_id": type_id(user_client, "fund")}
    assert_error(user_client.post(f"{API}/accounts", json=body), 403, "CSRF_ERROR")
    r = user_client.post(f"{API}/accounts", json=body, headers={"X-CSRF-Token": "wrong"})
    assert_error(r, 403, "CSRF_ERROR")
    r = user_client.post(f"{API}/accounts", json=body, headers={"X-CSRF-Token": good})
    assert r.status_code == 201


def test_csrf_required_for_put_and_delete(user_client):
    acc = create_account(user_client, "fund", "Фонд")
    good = user_client.headers.pop("X-CSRF-Token")
    assert_error(user_client.delete(f"{API}/accounts/{acc['id']}"), 403, "CSRF_ERROR")
    r = user_client.put(
        f"{API}/accounts/{acc['id']}",
        json={"name": "y", "account_type_id": acc["account_type"]["id"]},
    )
    assert_error(r, 403, "CSRF_ERROR")
    r = user_client.delete(f"{API}/accounts/{acc['id']}", headers={"X-CSRF-Token": good})
    assert r.status_code == 204


def test_csrf_token_of_other_session_rejected(user_client, other_client):
    foreign_token = other_client.headers["X-CSRF-Token"]
    r = user_client.post(
        f"{API}/accounts",
        json={"name": "x", "account_type_id": 1},
        headers={"X-CSRF-Token": foreign_token},
    )
    assert_error(r, 403, "CSRF_ERROR")


def test_anonymous_login_requires_csrf(client, user):
    r = client.post(f"{API}/auth/login", json={"email": user.email, "password": PASSWORD})
    assert_error(r, 403, "CSRF_ERROR")


# ---------------------------------------------------------------- регистрация и вход


def test_register_login_me_logout(client):
    token = anon_csrf(client)
    r = client.post(
        f"{API}/auth/register",
        json={"email": "New.User@Example.com", "password": "Passw0rd!", "display_name": "Нов"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "new.user@example.com"
    assert r.json()["role"] == "user"

    body = login(client, "NEW.user@example.com", "Passw0rd!")
    assert body["user"]["display_name"] == "Нов"
    me = client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "new.user@example.com"
    # новому пользователю создан счёт «Нераспределённый доход»
    accs = client.get(f"{API}/accounts").json()
    assert [a["account_type"]["code"] for a in accs] == ["unallocated"]

    assert client.post(f"{API}/auth/logout").status_code == 204
    assert_error(client.get(f"{API}/auth/me"), 401, "UNAUTHORIZED")


def test_register_duplicate_email(client, user):
    r = client.post(
        f"{API}/auth/register",
        json={"email": user.email.upper(), "password": "Passw0rd!", "display_name": "X"},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert_error(r, 409, "CONFLICT", "email")


def test_register_invalid_email_message_in_russian(client):
    r = client.post(
        f"{API}/auth/register",
        json={"email": "not-an-email", "password": "Passw0rd!", "display_name": "X"},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert_error(r, 422, "VALIDATION_ERROR", "email")
    assert r.json()["error"]["message"] == "Некорректный адрес email"


def test_admin_cannot_reset_own_password(admin_client, admin):
    r = admin_client.post(f"{API}/admin/users/{admin.id}/reset-password")
    assert_error(r, 409, "CONFLICT")


def test_register_weak_password(client):
    r = client.post(
        f"{API}/auth/register",
        json={"email": "w@example.com", "password": "12345678", "display_name": "X"},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert_error(r, 422, "VALIDATION_ERROR", "password")


def test_login_wrong_password_and_unknown_email(client, user):
    token = anon_csrf(client)
    r = client.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": "wrong-pass1"},
        headers={"X-CSRF-Token": token},
    )
    err = assert_error(r, 401, "UNAUTHORIZED")
    r = client.post(
        f"{API}/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-pass1"},
        headers={"X-CSRF-Token": token},
    )
    # одинаковое сообщение — не раскрываем, существует ли email
    assert assert_error(r, 401, "UNAUTHORIZED")["message"] == err["message"]


def test_login_lockout_after_5_failures(client, user):
    token = anon_csrf(client)

    def attempt(password):
        return client.post(
            f"{API}/auth/login",
            json={"email": user.email, "password": password},
            headers={"X-CSRF-Token": token},
        )

    for _ in range(5):
        assert_error(attempt("wrong-pass1"), 401, "UNAUTHORIZED")
    # теперь даже верный пароль не принимается
    assert_error(attempt(PASSWORD), 429, "TOO_MANY_ATTEMPTS")


def test_successful_login_resets_failed_counter(client, user, db):
    token = anon_csrf(client)
    for _ in range(4):
        client.post(
            f"{API}/auth/login",
            json={"email": user.email, "password": "wrong-pass1"},
            headers={"X-CSRF-Token": token},
        )
    login(client, user.email)
    assert user.failed_login_count == 0
    assert user.locked_until is None


def test_update_profile_and_change_password(user_client, user, new_client):
    r = user_client.put(
        f"{API}/auth/me",
        json={"display_name": "Алиса Л.", "salary": "150000.50", "salary_day": 10},
    )
    assert r.status_code == 200, r.text
    assert D(r.json()["salary"]) == D("150000.50")

    second = new_client()
    login(second, user.email)

    r = user_client.post(
        f"{API}/auth/password",
        json={"current_password": "wrong-pass1", "new_password": "Another-pass2"},
    )
    assert_error(r, 422, "VALIDATION_ERROR", "current_password")
    r = user_client.post(
        f"{API}/auth/password",
        json={"current_password": PASSWORD, "new_password": "Another-pass2"},
    )
    assert r.status_code == 204
    # текущая сессия жива, остальные закрыты
    assert user_client.get(f"{API}/auth/me").status_code == 200
    assert_error(second.get(f"{API}/auth/me"), 401, "UNAUTHORIZED")
    login(second, user.email, "Another-pass2")


def test_registration_closed(admin_client, client):
    r = admin_client.put(f"{API}/admin/settings", json={"registration_open": False})
    assert r.status_code == 200
    assert r.json() == {"registration_open": False}
    r = client.post(
        f"{API}/auth/register",
        json={"email": "late@example.com", "password": "Passw0rd!", "display_name": "X"},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert_error(r, 403, "FORBIDDEN")


# ---------------------------------------------------------------- разграничение доступа


def test_user_cannot_access_other_users_data(user_client, other_client):
    acc = create_account(user_client, "current", "Карта")
    plan = create_income_plan(user_client)
    op = user_client.post(
        f"{API}/operations",
        json={
            "operation_type": "income",
            "op_date": "2026-01-10",
            "amount": "1000",
            "plan_id": plan["id"],
        },
    ).json()

    for path, body in (
        (f"/accounts/{acc['id']}", {"name": "hack", "account_type_id": acc["account_type"]["id"]}),
        (
            f"/operations/{op['id']}",
            {
                "operation_type": "income",
                "op_date": "2026-01-10",
                "amount": "1",
                "plan_id": plan["id"],
            },
        ),
        (
            f"/planning/{plan['id']}",
            {"operation_type": "expense", "planned_date": "2026-01-10", "amount_planned": "1"},
        ),
    ):
        assert_error(other_client.get(f"{API}{path}"), 404, "NOT_FOUND")
        assert other_client.put(f"{API}{path}", json=body).status_code == 404
        assert_error(other_client.delete(f"{API}{path}"), 404, "NOT_FOUND")

    assert other_client.post(f"{API}/accounts/{acc['id']}/close").status_code == 404
    assert other_client.get(f"{API}/accounts/{acc['id']}/replenishments").status_code == 404
    # списки не содержат чужих данных
    other_ids = {a["id"] for a in other_client.get(f"{API}/accounts").json()}
    assert acc["id"] not in other_ids
    assert other_client.get(f"{API}/operations").json()["total"] == 0
    assert other_client.get(f"{API}/planning?year=2026").json() == []
    # фильтр по чужому счёту ничего не возвращает
    assert other_client.get(f"{API}/operations?account_id={acc['id']}").json()["total"] == 0

    # данные владельца не изменились
    assert user_client.get(f"{API}/accounts/{acc['id']}").json()["name"] == "Карта"
    assert user_client.get(f"{API}/operations/{op['id']}").status_code == 200
    assert user_client.get(f"{API}/planning/{plan['id']}").status_code == 200


def test_cannot_link_operation_to_other_users_account(user_client, other_client):
    foreign = create_account(other_client, "current", "Чужая")
    r = user_client.post(
        f"{API}/operations",
        json={
            "operation_type": "expense",
            "account_id": foreign["id"],
            "op_date": "2026-01-10",
            "amount": "1",
            "name": "x",
        },
    )
    assert_error(r, 404, "NOT_FOUND", "account_id")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/users"),
        ("get", "/admin/settings"),
        ("put", "/admin/settings"),
        ("get", "/admin/tax-brackets"),
        ("post", "/admin/holidays"),
        ("get", "/admin/income-kinds"),
        ("post", f"/admin/users/{uuid.uuid4()}/reset-password"),
    ],
)
def test_admin_endpoints_forbidden_for_user(user_client, method, path):
    r = (
        getattr(user_client, method)(f"{API}{path}")
        if method == "get"
        else user_client.request(method.upper(), f"{API}{path}", json={})
    )
    assert_error(r, 403, "FORBIDDEN")


def test_admin_endpoints_unauthorized_for_anonymous(client):
    assert_error(client.get(f"{API}/admin/users"), 401, "UNAUTHORIZED")


def test_admin_cannot_see_users_financial_data(admin_client, user_client, user):
    user_client.put(
        f"{API}/auth/me", json={"display_name": "A", "salary": "99999", "salary_day": 5}
    )
    acc = create_account(user_client, "current", "Секретный счёт")

    users = admin_client.get(f"{API}/admin/users").json()
    row = next(u for u in users if u["email"] == user.email)
    assert set(row) == {"id", "email", "display_name", "role", "is_active", "must_change_password"}
    assert not any(k in row for k in ("salary", "salary_day", "advance_day", "advance_calc_day"))

    # админ видит только свои счета и не может прочитать чужой
    admin_accounts = admin_client.get(f"{API}/accounts").json()
    assert acc["id"] not in {a["id"] for a in admin_accounts}
    assert_error(admin_client.get(f"{API}/accounts/{acc['id']}"), 404, "NOT_FOUND")
    assert admin_client.get(f"{API}/operations").json()["total"] == 0


def test_admin_reset_password_flow(admin_client, user_client, user, new_client):
    r = admin_client.post(f"{API}/admin/users/{user.id}/reset-password")
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert len(temp) >= 8

    # старая сессия пользователя аннулирована
    assert_error(user_client.get(f"{API}/auth/me"), 401, "UNAUTHORIZED")
    # старый пароль больше не подходит
    c = new_client()
    r = c.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},
        headers={"X-CSRF-Token": anon_csrf(c)},
    )
    assert r.status_code == 401

    body = login(c, user.email, temp)
    assert body["user"]["must_change_password"] is True
    assert c.get(f"{API}/auth/me").status_code == 200
    for path in ("/accounts", "/operations", "/planning", "/summary/balances"):
        assert_error(c.get(f"{API}{path}"), 403, "PASSWORD_CHANGE_REQUIRED")
    # профиль с временным паролем можно только читать
    assert_error(
        c.put(f"{API}/auth/me", json={"display_name": "Взлом"}), 403, "PASSWORD_CHANGE_REQUIRED"
    )

    r = c.post(
        f"{API}/auth/password", json={"current_password": temp, "new_password": "Brand-new-pass9"}
    )
    assert r.status_code == 204
    assert c.get(f"{API}/accounts").status_code == 200
    assert c.get(f"{API}/auth/me").json()["must_change_password"] is False


def test_admin_unknown_user_reset(admin_client):
    assert_error(
        admin_client.post(f"{API}/admin/users/{uuid.uuid4()}/reset-password"), 404, "NOT_FOUND"
    )


def test_admin_block_user(admin_client, user_client, user):
    r = admin_client.put(f"{API}/admin/users/{user.id}/active", json={"is_active": False})
    assert r.status_code == 200
    assert_error(user_client.get(f"{API}/auth/me"), 401, "UNAUTHORIZED")


# ---------------------------------------------------------------- инъекции


INJECTION = "Robert'); DROP TABLE accounts;-- \" OR 1=1 /* %_ \\"


def test_sql_injection_strings_stored_literally(user_client):
    acc = create_account(user_client, "fund", INJECTION)
    assert acc["name"] == INJECTION.strip()
    fetched = user_client.get(f"{API}/accounts/{acc['id']}").json()
    assert fetched["name"] == INJECTION.strip()
    r = user_client.post(
        f"{API}/operations",
        json={
            "operation_type": "expense",
            "account_id": acc["id"],
            "op_date": "2026-01-10",
            "amount": "1",
            "name": INJECTION,
            "category": "' OR '1'='1",
            "comment": INJECTION,
        },
    )
    assert r.status_code == 201, r.text
    op = user_client.get(f"{API}/operations/{r.json()['id']}").json()
    assert op["category"] == "' OR '1'='1"
    assert op["comment"] == INJECTION.strip()
    # таблицы на месте
    assert len(user_client.get(f"{API}/accounts").json()) == 2


@pytest.mark.parametrize(
    "query",
    [
        "operation_type=income' OR '1'='1",
        "account_id=1 OR 1=1",
        "account_type_id=1;DROP TABLE operations",
        "date_from=2026-01-01' --",
        "limit=1;DELETE",
    ],
)
def test_sql_injection_in_filters_rejected(user_client, query):
    r = user_client.get(f"{API}/operations?{query}")
    assert_error(r, 422, "VALIDATION_ERROR", r.json()["error"]["field"])


def test_sql_injection_in_planning_filter_and_login(user_client, client):
    r = user_client.get(f"{API}/planning", params={"operation_type": "income' OR '1'='1"})
    assert r.status_code == 422
    r = client.post(
        f"{API}/auth/login",
        json={"email": "' OR 1=1 --", "password": "' OR '1'='1"},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert_error(r, 401, "UNAUTHORIZED")


# ---------------------------------------------------------------- полный сценарий


def test_full_flow(user_client):
    c = user_client
    r = c.put(
        f"{API}/auth/me", json={"display_name": "Алиса", "salary": "100000", "salary_day": 10}
    )
    assert r.status_code == 200, r.text

    r = c.post(f"{API}/planning/salary", json={"start_date": "2026-01-01", "replace": False})
    assert r.status_code == 200, r.text
    calc = r.json()
    assert (calc["created"], calc["replaced"], calc["skipped"]) == (12, 0, 0)
    jan = next(p for p in calc["plans"] if p["planned_date"] == "2026-01-10")
    assert D(jan["tax_rate"]) == D("13")
    assert D(jan["amount_net"]) == D("87000.00")

    current = create_account(c, "current", "Карта")
    food = create_account(c, "monthly", "Продукты")
    unalloc = next(
        a for a in c.get(f"{API}/accounts").json() if a["account_type"]["code"] == "unallocated"
    )

    r = c.post(
        f"{API}/operations",
        json={
            "operation_type": "income",
            "op_date": "2026-01-10",
            "amount": jan["amount_net"],
            "plan_id": jan["id"],
            "comment": "январская зарплата",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["account_id"] == unalloc["id"]

    r = c.post(
        f"{API}/operations",
        json={
            "operation_type": "transfer",
            "account_id": unalloc["id"],
            "target_account_id": current["id"],
            "op_date": "2026-01-11",
            "amount": "50000",
        },
    )
    assert r.status_code == 201, r.text
    r = c.post(
        f"{API}/operations",
        json={
            "operation_type": "transfer",
            "account_id": current["id"],
            "target_account_id": food["id"],
            "op_date": "2026-01-11",
            "amount": "20000",
        },
    )
    assert r.status_code == 201, r.text
    r = c.post(
        f"{API}/operations",
        json={
            "operation_type": "expense",
            "account_id": food["id"],
            "op_date": "2026-01-12",
            "amount": "3456.78",
            "name": "Супермаркет",
        },
    )
    assert r.status_code == 201, r.text

    summary = {
        s["account_type_name"]: D(s["balance"]) for s in c.get(f"{API}/summary/balances").json()
    }
    assert summary["Нераспределённый доход"] == D("37000.00")
    assert summary["Текущий"] == D("30000.00")
    assert summary["Ежемесячные траты"] == D("16543.22")
    assert summary["Фонд"] == D("0.00")
    assert summary["Накопления"] == D("0.00")

    balances = {a["name"]: D(a["balance"]) for a in c.get(f"{API}/accounts").json()}
    assert balances["Продукты"] == D("16543.22")

    plan = c.get(f"{API}/planning/{jan['id']}").json()
    assert D(plan["amount_fact"]) == D("87000.00")
    assert len(plan["operation_ids"]) == 1

    ops = c.get(f"{API}/operations", params={"operation_type": "transfer"}).json()
    assert ops["total"] == 2

    hist = c.get(f"{API}/accounts/{food['id']}/replenishments").json()
    assert [D(h["amount"]) for h in hist] == [D("20000.00")]

    # план с фактическими операциями удалить нельзя
    assert_error(c.delete(f"{API}/planning/{jan['id']}"), 409, "CONFLICT")
    # счёт с операциями удалить нельзя, закрыть с ненулевым остатком — тоже
    assert_error(c.delete(f"{API}/accounts/{food['id']}"), 409, "CONFLICT")
    assert_error(c.post(f"{API}/accounts/{food['id']}/close"), 409, "CONFLICT")


def test_reopen_fund_via_api(user_client):
    c = user_client
    cur = create_account(c, "current", "Карта")
    fund = create_account(c, "fund", "Отпуск")
    r = c.post(
        f"{API}/operations",
        json={
            "operation_type": "transfer",
            "account_id": cur["id"],
            "target_account_id": fund["id"],
            "op_date": "2026-01-11",
            "amount": "700",
        },
    )
    assert r.status_code == 201
    r = c.post(f"{API}/accounts/{fund['id']}/reopen")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_account_id"] == fund["id"]
    assert D(body["transferred"]) == D("700.00")
    assert D(body["new_account"]["balance"]) == D("700.00")
    assert body["new_account"]["name"] == "Отпуск"
    assert c.get(f"{API}/accounts/{fund['id']}").json()["is_closed"] is True
    r = c.post(f"{API}/accounts/{cur['id']}/reopen")
    assert_error(r, 422, "VALIDATION_ERROR")


def test_prior_income_api(user_client, other_client):
    url = f"{API}/planning/prior-income/2026"
    assert user_client.get(url).json() == {"year": 2026, "amount": "0.00"}
    r = user_client.put(url, json={"amount": "123456.78"})
    assert r.status_code == 200
    assert r.json()["amount"] == "123456.78"
    # у другого пользователя своё значение
    assert other_client.get(url).json()["amount"] == "0.00"
    assert_error(user_client.put(url, json={"amount": "-1"}), 422, "VALIDATION_ERROR", "amount")
    assert_error(
        user_client.get(f"{API}/planning/prior-income/1999"), 422, "VALIDATION_ERROR", "year"
    )
