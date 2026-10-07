"""A small CICS-style demo application served by the TN3270 simulator.

Screens: sign-on (with a non-display password field), main menu, customer
inquiry/update and a paged customer list. All data is fictional and kept in
memory, so every simulator instance starts from the same state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .datastream import InboundRecord, Input, ScreenDefinition, Text

DEFAULT_USERS = {"DEMO": "DEMO123"}
STATUSES = ("ACTIVE", "HOLD", "CLOSED")
PAGE_SIZE = 10


@dataclass
class Customer:
    number: str
    name: str
    address: str
    city: str
    phone: str
    status: str
    credit_limit: int
    balance: float
    last_payment: str


# fmt: off
SAMPLE_CUSTOMERS = [
    Customer("100001", "ACME TOOL & DIE CO", "1200 INDUSTRIAL PKWY", "CLEVELAND, OH", "216-555-0101", "ACTIVE", 50000, 12450.75, "2026-09-28"),
    Customer("100002", "BLUE RIVER BAKERY", "45 MAIN ST", "HANNIBAL, MO", "573-555-0102", "ACTIVE", 10000, 1820.00, "2026-09-30"),
    Customer("100003", "CASCADE OUTDOOR SUPPLY", "9800 SUMMIT AVE", "BEND, OR", "541-555-0103", "HOLD", 25000, 27310.40, "2026-06-15"),
    Customer("100004", "DELTA FREIGHT LINES", "77 HARBOR RD", "MOBILE, AL", "251-555-0104", "ACTIVE", 100000, 64002.10, "2026-10-01"),
    Customer("100005", "EVERGREEN DENTAL GROUP", "310 PINE ST", "OLYMPIA, WA", "360-555-0105", "ACTIVE", 15000, 0.00, "2026-09-12"),
    Customer("100006", "FRONTIER FEED & SEED", "RR 2 BOX 18", "AMARILLO, TX", "806-555-0106", "CLOSED", 0, 0.00, "2025-12-31"),
    Customer("100007", "GRANITE STATE PRINTING", "5 MILL ST", "NASHUA, NH", "603-555-0107", "ACTIVE", 20000, 4380.25, "2026-09-25"),
    Customer("100008", "HARBOR VIEW MARINA", "1 DOCK ST", "ANNAPOLIS, MD", "410-555-0108", "HOLD", 30000, 31875.00, "2026-05-02"),
    Customer("100009", "IRONWOOD CABINETRY", "88 OAK LN", "GRAND RAPIDS, MI", "616-555-0109", "ACTIVE", 40000, 18900.60, "2026-09-29"),
    Customer("100010", "JUNIPER HILL FARMS", "2300 COUNTY RD 9", "FORT COLLINS, CO", "970-555-0110", "ACTIVE", 12000, 2750.00, "2026-09-18"),
    Customer("100011", "KEYSTONE AUTO PARTS", "410 LINCOLN HWY", "YORK, PA", "717-555-0111", "ACTIVE", 35000, 9120.35, "2026-09-27"),
    Customer("100012", "LAKESIDE MEDICAL SUPPLY", "600 SHORE DR", "MADISON, WI", "608-555-0112", "ACTIVE", 60000, 22410.00, "2026-10-02"),
    Customer("100013", "MESA SOLAR INSTALLERS", "1500 SUN VALLEY RD", "TEMPE, AZ", "480-555-0113", "HOLD", 45000, 47220.90, "2026-04-20"),
    Customer("100014", "NORTHWIND COFFEE ROASTERS", "23 FRONT ST", "BURLINGTON, VT", "802-555-0114", "ACTIVE", 8000, 640.00, "2026-09-30"),
    Customer("100015", "OLD MILL HARDWARE", "12 RIVER RD", "LOWELL, MA", "978-555-0115", "ACTIVE", 18000, 5310.15, "2026-09-21"),
    Customer("100016", "PRAIRIE WIND ENERGY", "700 TURBINE WAY", "SIOUX FALLS, SD", "605-555-0116", "ACTIVE", 90000, 71005.00, "2026-09-30"),
    Customer("100017", "QUARRY STONE WORKS", "3 GRANITE RD", "BARRE, VT", "802-555-0117", "CLOSED", 0, 0.00, "2026-01-15"),
    Customer("100018", "REDWOOD COAST CATERING", "55 OCEAN AVE", "EUREKA, CA", "707-555-0118", "ACTIVE", 14000, 3305.50, "2026-09-26"),
    Customer("100019", "SILVER LAKE ELECTRIC", "820 VOLTAGE DR", "RENO, NV", "775-555-0119", "ACTIVE", 50000, 16780.00, "2026-09-24"),
    Customer("100020", "TIDEWATER SEAFOOD CO", "14 WHARF ST", "NORFOLK, VA", "757-555-0120", "HOLD", 20000, 20940.65, "2026-03-30"),
    Customer("100021", "UPLAND GRAIN COOPERATIVE", "1 ELEVATOR RD", "SALINA, KS", "785-555-0121", "ACTIVE", 75000, 38120.00, "2026-09-20"),
    Customer("100022", "VALLEY VIEW VETERINARY", "230 MEADOW LN", "BOISE, ID", "208-555-0122", "ACTIVE", 10000, 1275.80, "2026-09-29"),
    Customer("100023", "WILLOW CREEK TEXTILES", "400 LOOM ST", "GREENVILLE, SC", "864-555-0123", "ACTIVE", 30000, 11650.00, "2026-09-17"),
    Customer("100024", "YELLOWSTONE OUTFITTERS", "9 GATEWAY BLVD", "BOZEMAN, MT", "406-555-0124", "ACTIVE", 22000, 7480.30, "2026-09-23"),
]
# fmt: on


class CustomerStore:
    """In-memory customer master file shared by all sessions of one simulator."""

    def __init__(self, customers: list[Customer] | None = None) -> None:
        source = SAMPLE_CUSTOMERS if customers is None else customers
        self._customers = {c.number: copy.copy(c) for c in source}

    def get(self, number: str) -> Customer | None:
        return self._customers.get(number)

    def all(self) -> list[Customer]:
        return [self._customers[k] for k in sorted(self._customers)]


class DemoApplication:
    """State machine for one terminal session."""

    def __init__(
        self,
        store: CustomerStore,
        users: dict[str, str] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.store = store
        self.users = {k.upper(): v for k, v in (users or DEFAULT_USERS).items()}
        self.clock = clock
        self.state = "signon"
        self.userid = ""
        self.message = ""
        self.closed = False
        self.page = 0
        self.custno = ""
        self.customer: Customer | None = None
        self._screen: ScreenDefinition | None = None

    # ----------------------------------------------------------------- public

    def start(self) -> ScreenDefinition:
        """Return the first screen shown after the terminal connects."""
        return self._render()

    def handle(self, inbound: InboundRecord) -> ScreenDefinition:
        """Process one inbound record and return the next screen."""
        inputs = self._screen.inputs() if self._screen else {}
        values = {inputs[a].name: v for a, v in inbound.fields.items() if a in inputs}
        aid = inbound.aid_name
        self.message = ""
        if aid == "CLEAR":
            return self._render()
        handler = getattr(self, f"_on_{self.state}")
        handler(aid, values)
        return self._render()

    # --------------------------------------------------------------- handlers

    def _on_signon(self, aid: str, values: dict[str, str]) -> None:
        if aid == "PF3":
            self.state = "goodbye"
            self.closed = True
            return
        if aid != "ENTER":
            self.message = f"Function key not active: {aid}."
            return
        userid = values.get("userid", self.userid).strip().upper()
        password = values.get("password", "").strip()
        self.userid = userid
        if not userid or not password:
            self.message = "Type your userid and password, then press ENTER."
        elif userid in self.users and password.upper() == self.users[userid].upper():
            self.state = "menu"
            self.message = f"Sign-on complete. Welcome, {userid}."
        else:
            self.message = "DFHCE3530 Invalid userid or password. Try again."

    def _on_menu(self, aid: str, values: dict[str, str]) -> None:
        if aid == "PF3":
            self._sign_off()
            return
        if aid != "ENTER":
            self.message = f"Function key not active: {aid}."
            return
        option = values.get("option", "").strip().upper()
        if option == "1":
            self.state = "customer"
            self.custno = ""
            self.customer = None
            self.message = "Type a customer number and press ENTER."
        elif option == "2":
            self.state = "list"
            self.page = 0
        elif option == "X":
            self._sign_off()
        elif not option:
            self.message = "Type an option and press ENTER."
        else:
            self.message = f"Invalid option: {option}."

    def _on_customer(self, aid: str, values: dict[str, str]) -> None:
        if aid == "PF3":
            self.state = "menu"
            return
        if aid == "PF12":
            self.custno = ""
            self.customer = None
            self.message = "Cancelled. Type a customer number and press ENTER."
            return
        if aid == "ENTER":
            custno = values.get("custno", self.custno).strip()
            self.custno = custno
            if len(custno) != 6 or not custno.isdigit():
                self.customer = None
                self.message = "Customer number must be 6 digits."
                return
            self.customer = self.store.get(custno)
            if self.customer is None:
                self.message = f"Customer {custno} not found."
            else:
                self.message = f"Customer {custno} displayed. Change phone/status and press F5 to save."
            return
        if aid == "PF5":
            self._save_customer(values)
            return
        self.message = f"Function key not active: {aid}."

    def _save_customer(self, values: dict[str, str]) -> None:
        customer = self.customer
        if customer is None:
            self.message = "Display a customer (ENTER) before saving."
            return
        custno = values.get("custno", self.custno).strip()
        if custno != customer.number:
            self.custno = custno
            self.message = "Customer number changed. Press ENTER to display it first."
            return
        phone = values.get("phone", customer.phone).strip()
        status = values.get("status", customer.status).strip().upper()
        if status not in STATUSES:
            self.message = f"Invalid status {status!r}. Use ACTIVE, HOLD or CLOSED."
            return
        if not phone or any(ch not in "0123456789- " for ch in phone):
            self.message = "Phone may only contain digits, spaces and dashes."
            return
        customer.phone = phone
        customer.status = status
        self.message = f"Customer {customer.number} updated."

    def _on_list(self, aid: str, values: dict[str, str]) -> None:
        pages = self._page_count()
        if aid == "PF3":
            self.state = "menu"
        elif aid == "PF8":
            if self.page + 1 >= pages:
                self.message = "Bottom of list."
            else:
                self.page += 1
        elif aid == "PF7":
            if self.page == 0:
                self.message = "Top of list."
            else:
                self.page -= 1
        elif aid == "ENTER":
            self.message = "Use F7/F8 to scroll, F3 to return."
        else:
            self.message = f"Function key not active: {aid}."

    def _on_goodbye(self, aid: str, values: dict[str, str]) -> None:
        self.closed = True

    def _sign_off(self) -> None:
        self.state = "signon"
        self.userid = ""
        self.customer = None
        self.custno = ""
        self.message = "Sign-off complete."

    # -------------------------------------------------------------- rendering

    def _page_count(self) -> int:
        return max(1, -(-len(self.store.all()) // PAGE_SIZE))

    def _header(self, map_id: str, title: str) -> list[Text | Input]:
        now = self.clock()
        items: list[Text | Input] = [
            Text(1, 2, map_id),
            Text(1, 41 - len(title) // 2, title, bright=True),
            Text(1, 71, now.strftime("%m/%d/%y")),
            Text(2, 71, now.strftime("%H:%M:%S")),
        ]
        if self.state != "signon" and self.userid:
            items.append(Text(2, 2, f"User: {self.userid}"))
        return items

    def _footer(self, keys: str) -> list[Text | Input]:
        items: list[Text | Input] = [Text(24, 2, keys)]
        if self.message:
            items.insert(0, Text(23, 2, self.message[:78], bright=True))
        return items

    def _render(self) -> ScreenDefinition:
        screen = getattr(self, f"_screen_{self.state}")()
        self._screen = screen
        return screen

    def _screen_signon(self) -> ScreenDefinition:
        items = self._header("GSSIGN", "GREEN SCREEN DEMO - SIGN ON")
        items += [
            Text(4, 2, "Welcome to the Green Screen demo region. Authorized users only."),
            Text(6, 2, "Type your userid and password, then press ENTER."),
            Text(8, 4, "Userid . . . . :"),
            Input("userid", 8, 22, 8, value=self.userid),
            Text(9, 4, "Password . . . :"),
            Input("password", 9, 22, 8, hidden=True),
        ]
        items += self._footer("ENTER=Sign on  F3=Exit")
        cursor = "password" if self.userid else "userid"
        return ScreenDefinition(items, cursor_field=cursor)

    def _screen_goodbye(self) -> ScreenDefinition:
        items = self._header("GSSIGN", "GREEN SCREEN DEMO")
        items.append(Text(12, 2, "Session ended. Goodbye."))
        return ScreenDefinition(items)

    def _screen_menu(self) -> ScreenDefinition:
        items = self._header("GSMENU", "GREEN SCREEN DEMO - MAIN MENU")
        items += [
            Text(4, 2, "Select an option and press ENTER."),
            Text(6, 6, "1  Customer inquiry / update"),
            Text(7, 6, "2  Customer list"),
            Text(8, 6, "X  Sign off"),
            Text(10, 2, "Option ===>"),
            Input("option", 10, 15, 2),
        ]
        items += self._footer("ENTER=Select  F3=Sign off")
        return ScreenDefinition(items, cursor_field="option")

    def _screen_customer(self) -> ScreenDefinition:
        c = self.customer
        items = self._header("GSCUST", "CUSTOMER INQUIRY / UPDATE")
        items += [
            Text(4, 2, "Customer number . . :"),
            Input("custno", 4, 25, 6, value=self.custno, numeric=True),
            Text(4, 33, "(6 digits, then ENTER)"),
            Text(6, 2, "Name . . . . . . . :"),
            Text(7, 2, "Address  . . . . . :"),
            Text(8, 2, "City . . . . . . . :"),
            Text(9, 2, "Phone  . . . . . . :"),
            Text(10, 2, "Status . . . . . . :"),
            Text(11, 2, "Credit limit . . . :"),
            Text(12, 2, "Balance  . . . . . :"),
            Text(13, 2, "Last payment . . . :"),
        ]
        if c is not None:
            items += [
                Text(6, 25, c.name),
                Text(7, 25, c.address),
                Text(8, 25, c.city),
                Input("phone", 9, 25, 12, value=c.phone),
                Input("status", 10, 25, 6, value=c.status),
                Text(10, 33, "(ACTIVE, HOLD or CLOSED)"),
                Text(11, 25, f"{c.credit_limit:,.2f}"),
                Text(12, 25, f"{c.balance:,.2f}"),
                Text(13, 25, c.last_payment),
            ]
        items += self._footer("ENTER=Inquire  F3=Return  F5=Save  F12=Cancel")
        return ScreenDefinition(items, cursor_field="custno")

    def _screen_list(self) -> ScreenDefinition:
        customers = self.store.all()
        pages = self._page_count()
        self.page = min(self.page, pages - 1)
        items = self._header("GSLIST", "CUSTOMER LIST")
        items += [
            Text(3, 2, f"Page {self.page + 1} of {pages}"),
            Text(5, 2, "Cust#   Name                       City               Status        Balance"),
            Text(6, 2, "------  -------------------------  -----------------  ------  -------------"),
        ]
        start = self.page * PAGE_SIZE
        for offset, c in enumerate(customers[start : start + PAGE_SIZE]):
            line = f"{c.number}  {c.name:<25.25}  {c.city:<17.17}  {c.status:<6}  {c.balance:>13,.2f}"
            items.append(Text(7 + offset, 2, line))
        items += self._footer("F3=Return  F7=Backward  F8=Forward")
        return ScreenDefinition(items)
