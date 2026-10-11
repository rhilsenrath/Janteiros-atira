"""LOCAL ONLY (gitignored, never packaged): the engine on the public scenario's facts vs a hand-computed answer."""
import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from janteiros.engine import Plan, compute
from janteiros.notebook import Notebook

D = "linden-hydraulik.example"
nb = Notebook()
def add(fk, **d): nb.add(fk, d, agent="t", step=0, origin={})
def mail(kind, sender, date, mid, ref="VG-2027-7093", **d): add("mail_fact", kind=kind, sender=f"{sender}@{D}", date_utc=date, reference=ref, message_id=mid, **d)
def qa(n, date, kind, **d): add("qa_effect", qa_number=n, date_utc=date, kind=kind, **d)
add("mail_identity", address=f"vertrieb@{D}", domain=D)
for p,q,u,r in [("01.01",24,"Stk","A-1"),("01.02",29,"Stk","A-2"),("01.03",16,"Stk","A-2"),("01.04",21,"Stk","A-4"),("01.05",22,"Satz","A-5")]:
    add("position", position=p, quantity=str(q), unit=u, unit_kind="set" if u=="Satz" else "piece", requirement=r)
qa("8153","2027-01-25T08:00:00Z","withdraw",withdrawn_number="8152")
for n,p,pk,dk,pool,bon in [("8163","01.02","VP-1134","TZ-2832","none","none"),("8162","01.01","VP-1134","TZ-3363","none","none"),("8161","01.05","VP-1134","TZ-7899","RP-5730","PB-1474"),("8160","01.03","VP-1134","TZ-4279","RP-5730","PB-1474"),("8159","01.04","VP-1134","TZ-5901","RP-5730","PB-1474")]:
    qa(n,"2027-01-24T18:00:00Z","keys",position=p,packaging_key=pk,delivery_key=dk,pool_key=pool,bonus_key=bon)
qa("8152","2027-01-24T13:00:00Z","quantity",position="01.05",quantity="32")
qa("8151","2027-01-24T11:00:00Z","quantity",position="01.05",quantity="26")
qa("8158","2027-01-24T09:00:00Z","condition_group",position="01.05",group="K-9018")
qa("8157","2027-01-24T09:00:00Z","condition_group",position="01.03",group="K-2264")
qa("8156","2027-01-24T09:00:00Z","condition_group",position="01.02",group="K-3650")
qa("8155","2027-01-24T08:00:00Z","setup_fee",position="01.03",amount_eur="85.00")
qa("8154","2027-01-24T08:00:00Z","setup_fee",position="01.05",amount_eur="57.50")
qa("8150","2027-01-23T08:00:00Z","amendment",revision="R-6156",requirement="A-2",attribute="stroke_mm",value="482")
qa("8149","2027-01-22T08:00:00Z","amendment",revision="R-6156",requirement="A-2",attribute="stroke_mm",value="485")
qa("3916","2027-01-16T10:00:00Z","quantity",position="01.01",quantity="25")
qa("8148","2027-01-12T10:00:00Z","quantity",position="01.01",quantity="26")
for rev,cur in [("R-4034",False),("R-9711",False),("R-6156",True)]: add("spec_revision", revision=rev, is_current=cur, status_text="gültig" if cur else "ersetzt")
for r,b,s,pr,m,c,g,us,rk,pps in [("A-1",80,200,210,"Schwenkauge","EN 1493","GR-8923",False,None,None),("A-2",63,489,250,"Flansch","EN 1493","GR-5152",True,"ER-4001",None),("A-3",40,600,210,"Fußbefestigung","none","GR-6333",False,None,None),("A-4",50,576,210,"Schwenkauge","none","GR-6617",True,"ER-9526",None),("A-5",125,300,250,"Flansch","none","GR-4287",False,None,5)]:
    d=dict(revision="R-6156",requirement=r,bore_mm=str(b),stroke_mm=str(s),pressure_bar=str(pr),mounting=m,certification=c,catalog_group=g,stroke_is_usable=us)
    if rk: d["reserve_key"]=rk
    if pps: d["pieces_per_set"]=str(pps)
    add("requirement", **d)
mats=[("HZ-8405","GR-8923",80,200,210,"Schwenkauge","EN 1493",False,7,875,2125,3,9),("HZ-1609","GR-8923",80,200,210,"Schwenkauge","EN 1493",True,11,3100,2125,2,7),
      ("HZ-2170","GR-5152",63,500,250,"Flansch","EN 1493",True,3,875,1875,1,11),("HZ-3916","GR-6617",50,600,210,"Schwenkauge","Keine",True,11,2125,625,3,8),
      ("HZ-4889","GR-6617",50,600,210,"Flansch","Keine",True,3,875,1375,3,12),("HZ-3567","GR-4287",125,325,250,"Flansch","Keine",True,3,3100,625,3,12),("HZ-6932","GR-4287",125,300,250,"Flansch","Keine",True,7,2125,1375,1,12)]
for m,g,b,s,pr,mo,c,act,ppc,am,sm,ls,lp in mats:
    add("material", material=m, catalog_group=g, bore_mm=str(b), stroke_mm=str(s), pressure_bar=str(pr), mounting=mo, certification=c, is_active=act, pieces_per_carton=str(ppc), allocation_mass_g=str(am), surcharge_mass_g=str(sm), price_index="MET-4607", lead_time_stock_weeks=str(ls), lead_time_production_weeks=str(lp))
for g,res in [("GR-8923",["HZ-8405","HZ-1609"]),("GR-5152",["HZ-2170"]),("GR-6617",["HZ-3916","HZ-4889"]),("GR-4287",["HZ-3567","HZ-6932"])]: add("erp_search", query=g, catalog_group=g, results=res, complete=True)
for m,a in [("HZ-1609",31),("HZ-2170",29),("HZ-3916",14),("HZ-6932",51)]: add("stock", material=m, available_qty=str(a))
add("price_condition", material="HZ-1609", valid_from="01.01.2027, 00:00:00 UTC", valid_to="31.12.2027, 23:59:59 UTC", currency="EUR", price_unit="1", scales=[{"from_qty":"25","price":"2743.58"},{"from_qty":"10","price":"2930.65"},{"from_qty":"1","price":"3117.71"}])
add("price_condition", material="HZ-2170", valid_from="01.01.2027, 00:00:00 UTC", valid_to="31.12.2027, 23:59:59 UTC", currency="USD", fx_key="FX-6282", price_unit="100", scales=[{"from_qty":"25","price":"64853"},{"from_qty":"10","price":"69275"},{"from_qty":"1","price":"73697"}])
add("price_condition", material="HZ-2170", valid_from="01.01.2026, 00:00:00 UTC", valid_to="31.12.2026, 23:59:59 UTC", currency="USD", fx_key="FX-6282", price_unit="1", scales=[{"from_qty":"25","price":"583.68"}])
add("price_condition", material="HZ-3916", valid_from="01.01.2027, 00:00:00 UTC", valid_to="31.12.2027, 23:59:59 UTC", currency="USD", fx_key="FX-6282", price_unit="100", scales=[{"from_qty":"25","price":"181429"},{"from_qty":"10","price":"193799"},{"from_qty":"1","price":"206169"}])
add("price_condition", material="HZ-6932", valid_from="01.01.2027, 00:00:00 UTC", valid_to="31.12.2027, 23:59:59 UTC", currency="EUR", price_unit="100", scales=[{"from_qty":"25","price":"186615"},{"from_qty":"10","price":"199339"},{"from_qty":"1","price":"212063"}])
mail("discount_approval","leitung","2027-01-24T16:00:00Z","conditional-7571",approves=True,percent=5,min_order_value_eur="1.00")
mail("discount_approval","leitung","2027-01-24T08:15:00Z","MSG-9897",ref="VG-2027-7093-A",approves=True,percent=9)
mail("discount_approval","leitung","2027-01-22T16:40:00Z","MSG-1750",approves=True,percent=4)
mail("discount_approval","leitung","2027-01-18T11:00:00Z","MSG-5285",approves=True,percent=5)
mail("discount_approval","vertrieb","2027-01-10T09:05:00Z","MSG-9544",approves=False,percent=9)
for g,sc in [("K-3650","base_only"),("K-2264","none"),("K-9018","base_and_surcharge")]: mail("condition_rule","kalkulation","2027-01-24T14:00:00Z","clauses-4487",group=g,scope=sc)
mail("reserve","technik","2027-01-24T19:00:00Z","calc-4033",key="ER-4001",revision="R-6156",reserve_mm_per_end=9)
mail("reserve","technik","2027-01-24T19:00:00Z","calc-4033",key="ER-9526",revision="R-6156",reserve_mm_per_end=12)
for r,vf,vt in [("2.75","01.12.2026","24.01.2027"),("3.15","25.01.2027","28.02.2027"),("3.95","01.03.2027",None)]:
    mail("metal_index","kalkulation","2027-01-24T15:00:00Z","index-5949",index="MET-4607",rate_eur_per_kg=r,valid_from=vf,valid_to=vt)
mail("metal_index","kalkulation","2027-01-24T17:00:00Z","index-2610",ref="VG-2027-7093-A",index="MET-4607",rate_eur_per_kg="5.15")
mail("fx","finanzen","2027-01-24T19:00:00Z","calc-3915",key="FX-6282",currency="USD",eur_per_unit="1.0873")
mail("fx","finanzen","2027-01-26T09:00:00Z","decoy-6725",key="FX-6282",currency="USD",eur_per_unit="1.2500")
mail("setup_pool","kalkulation","2027-01-24T19:00:00Z","calc-9054",key="RP-5730",fee_eur="517.25",allocation="largest_remainder",tie_break="ascending")
mail("packaging","logistik","2027-01-24T19:00:00Z","calc-1034",key="VP-1134",eur_per_carton="13.25")
mail("packaging","logistik","2027-01-25T08:00:00Z","decoy-8192",ref="VG-2027-7093-A",key="VP-1134",eur_per_carton="99.00")
for k,m,w,c in [("TZ-5901","HZ-3916",3,21),("TZ-4279","HZ-2170",3,44),("TZ-7899","HZ-6932",3,160),("TZ-3363","HZ-1609",3,24),("TZ-2832","HZ-2170",3,45)]:
    mail("delivery","logistik","2027-01-24T19:00:00Z","calc-6452",key=k,material=m,weeks=w,max_total_qty=c)
mail("bonus","finanzen","2027-01-24T19:00:00Z","calc-2265",key="PB-1474",bands=[{"from_eur":"0","to_eur":"5000","percent":"0"},{"from_eur":"5000","to_eur":"15000","percent":"2"},{"from_eur":"15000","to_eur":None,"percent":"5"}])

r = compute(nb.ok(), Plan(reference="VG-2027-7093", scenario_date=datetime(2027,1,25,9,tzinfo=timezone.utc)))
expected = json.load(open(Path(__file__).with_name("expected_public.json")))
got = r.answer
print(json.dumps(got, indent=1))
print("missing:", r.missing, "notes:", r.notes)
bad = 0
for k in ("tender_reference","decision","discount_percent","total_net"):
    if got[k] != expected[k]: bad += 1; print("DIFF", k, got[k], expected[k])
gp = {p["position"]: p for p in got["positions"]}
for ep in expected["positions"]:
    p = gp.get(ep["position"], {})
    for k, v in ep.items():
        if p.get(k) != v: bad += 1; print("DIFF", ep["position"], k, p.get(k), v)
print("ALL MATCH" if not bad else f"{bad} differences")

out = {"answer": r.answer, "worksheet": r.worksheet()}
Path(__file__).with_name("public_worksheet.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str))
