"""Regenerate the repetitive fixture pages: list.html, table.html, huge.html.

Run from repo root:  python tests/fixtures/generate.py
Deterministic output — safe to re-run; commit the results.
"""

from __future__ import annotations

from pathlib import Path

PAGES = Path(__file__).parent / "pages"

ADJECTIVES = ["Bella", "Breville", "Gaggia", "Rancilio", "DeLonghi", "Flair", "Cuisinart", "Krups"]
NOUNS = [
    "Espresso Machine",
    "Moka Pot",
    "Burr Grinder",
    "Milk Frother",
    "Scale",
    "Kettle",
    "Portafilter",
    "Tamper",
]
CITIES = [
    "Turin",
    "Milan",
    "Naples",
    "Seoul",
    "Portland",
    "Vienna",
    "Melbourne",
    "Osaka",
    "Lisbon",
    "Oslo",
]


def gen_list(n: int = 32) -> str:
    cards = []
    for i in range(n):
        name = (
            f"{ADJECTIVES[i % 8]} {NOUNS[(i // 3) % 8]} {['Pro', 'Classic', 'Mini', 'Max'][i % 4]}"
        )
        price = 19.99 + (i * 13.7) % 380
        rating = 3.0 + (i * 7 % 20) / 10
        reviews = 12 + (i * 137) % 4200
        cards.append(f"""      <div class="card">
        <img src="p{i}.png" alt="{name}" width="160" height="120">
        <h3><a href="/product.html?id={1000 + i}">{name}</a></h3>
        <p class="price">${price:.2f}</p>
        <p class="rating">{rating:.1f}★ ({reviews:,})</p>
        <button type="button" data-id="{1000 + i}">Add to cart</button>
      </div>""")
    cards_html = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Espresso Gear — Fixture Shop</title>
  <style>
    body {{ margin: 0; font-family: sans-serif; }}
    header {{ background: #1a3; color: #fff; padding: 12px 24px; display: flex; gap: 16px; }}
    header a {{ color: #fff; margin-right: 12px; }}
    .searchbox {{ margin-left: auto; }}
    main {{ display: flex; max-width: 1100px; margin: 0 auto; padding: 24px; gap: 24px; }}
    aside {{ width: 200px; flex-shrink: 0; }}
    aside div {{ margin-bottom: 10px; }}
    #grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; flex: 1; }}
    .card {{ border: 1px solid #ddd; padding: 10px; }}
    .pagination {{ text-align: center; padding: 24px; }}
    .pagination a {{ margin: 0 6px; }}
  </style>
</head>
<body>
  <header>
    <a href="/">Fixture Shop</a>
    <nav aria-label="Main"><a href="/list.html">Products</a> <a href="/deals.html">Deals</a> <a href="/help.html">Help</a></nav>
    <div class="searchbox">
      <input type="search" placeholder="Search products" aria-label="Search products">
      <button type="button">Search</button>
    </div>
  </header>
  <main>
    <aside aria-label="Filters">
      <h2>Filters</h2>
      <form id="filters">
        <h3>Brand</h3>
        <div><label><input type="checkbox" name="brand" value="bella"> Bella</label></div>
        <div><label><input type="checkbox" name="brand" value="breville"> Breville</label></div>
        <div><label><input type="checkbox" name="brand" value="gaggia"> Gaggia</label></div>
        <h3>Price</h3>
        <div><input type="text" name="min" placeholder="Min" size="6"> – <input type="text" name="max" placeholder="Max" size="6"></div>
        <h3>Rating</h3>
        <div><label><input type="checkbox" name="rating" value="4"> 4★ &amp; up</label></div>
        <button type="submit">Apply</button>
      </form>
    </aside>
    <section aria-label="Results">
      <h1>Espresso gear ({n} results)</h1>
      <div id="grid">
{cards_html}
      </div>
    </section>
  </main>
  <nav class="pagination" aria-label="Pagination">
    <a href="/list.html?page=1">1</a> <a href="/list.html?page=2">2</a>
    <a href="/list.html?page=3">3</a> <a href="/list.html?page=2">Next</a>
  </nav>
</body>
</html>
"""


def gen_table(rows: int = 25) -> str:
    body_rows = []
    for i in range(rows):
        status = ["Open", "Closed", "Pending"][i % 3]
        body_rows.append(f"""        <tr>
          <td>#{4400 + i}</td>
          <td><a href="/order.html?id={4400 + i}">Order from {CITIES[i % 10]}</a></td>
          <td>{(i * 3) % 28 + 1} Jun 2026</td>
          <td>${(24.5 + i * 31.3) % 900:.2f}</td>
          <td>{status}</td>
          <td><button type="button" data-order="{4400 + i}" class="ship">Ship</button>
              <button type="button" data-order="{4400 + i}" class="cancel">Cancel</button></td>
        </tr>""")
    rows_html = "\n".join(body_rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Orders — Fixture Admin</title>
  <style>
    body {{ margin: 0; font-family: sans-serif; }}
    header {{ background: #333; color: #fff; padding: 12px 24px; }}
    header a {{ color: #fff; margin-right: 12px; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
    th button {{ background: none; border: none; font-weight: bold; cursor: pointer; }}
  </style>
</head>
<body>
  <header>
    <a href="/">Fixture Admin</a>
    <nav aria-label="Main"><a href="/table.html">Orders</a> <a href="/customers.html">Customers</a> <a href="/reports.html">Reports</a></nav>
  </header>
  <main>
    <h1>Orders</h1>
    <form id="table-filter">
      <input type="search" placeholder="Filter orders" aria-label="Filter orders">
      <select aria-label="Status filter"><option>All</option><option>Open</option><option>Closed</option><option>Pending</option></select>
      <button type="submit">Filter</button>
    </form>
    <table>
      <thead>
        <tr>
          <th><button type="button" data-sort="id">ID ▲</button></th>
          <th><button type="button" data-sort="desc">Description</button></th>
          <th><button type="button" data-sort="date">Date</button></th>
          <th><button type="button" data-sort="total">Total</button></th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
{rows_html}
      </tbody>
    </table>
  </main>
</body>
</html>
"""


def gen_huge(n: int = 120) -> str:
    sections = []
    for i in range(n):
        sections.append(f"""    <section class="chunk">
      <h2>Chapter {i + 1}: Notes from {CITIES[i % 10]}</h2>
      <p>Entry {i + 1} in a very long travelogue. The espresso in {CITIES[i % 10]} was
      {["excellent", "forgettable", "surprising", "legendary"][i % 4]}, and the trains ran
      {["on time", "late", "early", "never"][i % 4]}.</p>
      <a href="/chapter{i + 1}.html">Read chapter {i + 1}</a>
    </section>""")
    sections_html = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>The Long Travelogue — Fixture</title>
  <style>
    body {{ margin: 0; font-family: sans-serif; }}
    main {{ max-width: 720px; margin: 0 auto; padding: 24px; }}
    .chunk {{ border-bottom: 1px solid #eee; padding: 12px 0; }}
  </style>
</head>
<body>
  <main>
    <h1>The Long Travelogue</h1>
{sections_html}
  </main>
</body>
</html>
"""


if __name__ == "__main__":
    (PAGES / "list.html").write_text(gen_list())
    (PAGES / "table.html").write_text(gen_table())
    (PAGES / "huge.html").write_text(gen_huge())
    print("wrote list.html, table.html, huge.html")
