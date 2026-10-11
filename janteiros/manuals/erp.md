You work the company's own ERP (often SAP-like: transaction codes, an F4 search help, tabs, status codes).
It holds materials, their technical data and status, stock, lead times and sales price conditions.

## Searching
- Find the material search (search help / F4 / material list). Search with the exact catalogue group
  (Kataloggruppe) your mission names; empty or broad searches list nothing on purpose; at most ~20 hits.
  The search may open in a pop-up window; the kit follows it.
- Record the search with `record_search` (every material listed, complete=true if you saw the whole list).

## Reading candidates
- For every listed candidate record what the result line shows (`record_materials`). Then open each
  candidate that could match the requirement (same bore, pressure, mounting and certification; the stroke can
  still change through amendments, so keep candidates whose stroke differs only slightly) and read:
  - technical data (bore, stroke, pressure, mounting, certification; certification may sit behind an ⓘ),
  - status (material status code and its legend; blocked / gesperrt materials do not qualify → is_active=false;
    released / freigegeben → is_active=true). Decoy codes may be hidden next to the real one: use the visible one,
  - lead times: from stock (ab Lager) and production (Produktion), in weeks,
  - pieces per carton (Packinhalt), allocation weight for setup pools (Umlagegewicht) and the separate mass for
    the metal surcharge (Zuschlagsmasse), both in grams per piece, and the metal price index code.
  Tabs (basic data, technical data, planning / Disposition) hold different parts: open each.
- For candidates that match: stock (stock overview / MMBE): drill down to storage locations; record the
  freely usable / available quantity and the blocked / inspection quantity separately (`record_stock`).
  Physical totals that include blocked or inspection stock are NOT the available stock.
- Price conditions (VK13 / conditions): there may be SEVERAL condition records (other validity periods, other
  price units): read every record (next record / pages). Quantity scales may sit in a separate register or tab
  of each record. Record each record with `record_condition`: validity from/to as shown, currency, FX key,
  price unit (Preiseinheit: per 1, per 100 …), the number format the page states (de-DE / en-US) and every
  scale with its minimum quantity and price copied character by character.

## Be fast (aim for about 8 turns)
1. Open the material search and search the catalogue group.
2. `record_search`, and in the same turn `read_pages` with every tab of every candidate (basic data, technical
   data, planning/status) — use the link targets you see, or their obvious pattern for the other candidates.
3. `record_materials` for all candidates at once (technical data, status code + is_active, lead times, pack size,
   both masses, price index), and in the same turn `read_pages` with the stock levels and every condition record
   (and its scale register) of the matching active candidates.
4. `record_stock` and one `record_condition` per condition record, then `done`.
Record from what you already read: do not read the same pages again.

## Notes
- The session times out quickly; the kit logs in again. Re-open your page and continue.
- Error pages ("temporarily unavailable") are retried by the kit; just look again.

## Done when
The search is recorded and, for every candidate that could match, its technical data, status, lead times,
pack size and masses are recorded; for matching active candidates also stock and all price condition records.
