You work the buyer's supplier portal (Lieferantenportal / customer portal) that holds the technical
specification (Lastenheft / technical data sheet) of the tender and its revisions.

## Finding the specification
- Look in the document area (register, library, downloads). Registers may be paged with very few entries per
  page; the right document mentions the tender reference or its title. Row actions may hide behind an
  "actions" menu button: open it, then the "show document" entry.
- Access forms (document release, Dokumentenüberlassung): read every option. Choose the purpose that gives
  you the binding / current documents (not "preview only", not "archive copy"), copy the receipt / document
  number exactly as displayed (it may only be shown as an image: «image: …»), tick the terms checkbox and
  request access. Never use "cancel / delete entries" buttons.

## The governing revision
- Open the revision history (it may be collapsed). The viewer often opens the OLDEST or a superseded revision
  first. The current one is the revision whose status says it is valid / current (gültig, aktuell,
  maßgeblich, freigegeben); "ersetzt / superseded / archiviert" are not. Status may sit in a tooltip.
- Record all revisions you see with `record_revisions` (is_current true only for the valid one).

## Requirements of the current revision
- Open the current revision. Requirement sheets may be paged (one requirement per page): use
  `read_all_pages` on the next/load-more control so you get all of them.
- For each requirement record with `record_requirements` (revision = the current one): bore (Bohrung, mm),
  stroke (Hub, mm), operating pressure (Betriebsdruck, bar), mounting (Befestigung), certification
  (Zertifizierung; 'none' if none is required), the catalogue group for the ERP search (Kataloggruppe), and:
  - stroke_is_usable = true when the text says the stroke is a usable travel (nutzbarer Weg) to which end
    reserves are added, plus the end-reserve key it names;
  - pieces_per_set when it states how many pieces a set (Satz) contains.
- Values may be given twice (table cell and original wording); they should agree. Copy numbers as shown.

## Done when
The current revision is identified and every requirement of that revision is recorded with all its values.
