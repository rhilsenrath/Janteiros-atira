"""Parse only explicitly labelled ERP prices and available-stock observations."""
import re
import unicodedata
from decimal import Decimal
from datetime import datetime,timezone
from urllib.parse import urlsplit,parse_qs
from core.contracts import PriceCondition


def material_fields(sources,requirement_id):
    """Read explicitly labelled MM03 values, without borrowing test requirements."""
    from core.contracts import TechnicalRequirement
    texts=[clean(s['text']) for s in sources if urlsplit(s['url']).path=='/mm03']
    text='\n'.join(texts)
    fields={}
    # Never trust the model's boolean: match the displayed code to its legend.
    codes=set(re.findall(r'Materialstatus\s*:?\s*(\d{2})\b',text))
    legend={}
    for code,label in re.findall(r'\b(\d{2})\s+(freigegeben|gesperrt)\b',text,re.I):
        legend.setdefault(code,set()).add(label.lower()=='freigegeben')
    fields['active']=None
    if len(codes)==1:
        meanings=legend.get(next(iter(codes)),set())
        if len(meanings)==1:fields['active']=next(iter(meanings))
    attributes={}
    for field,label in (('allocation_mass','Umlagegewicht'),('surcharge_mass','Zuschlagsmasse')):
        values=set(re.findall(label+r'\s+(\d+(?:[.,]\d+)?)\s*(kg|g)\s*/\s*(?:Stück|ST)',text))
        if len(values)==1:
            value,unit=values.pop()
            fields[field]=Decimal(value.replace(',','.'))
            attributes[field+'_unit']=unit
    cartons=set(re.findall(r'Packinhalt\s+(\d+)\s*Stück/Karton',text))
    if len(cartons)==1:fields['pieces_per_carton']=Decimal(cartons.pop())
    technical={}
    for field,label in (('bore','Bohrung'),('stroke','Hub'),('pressure','Betriebsdruck')):
        values=set(re.findall(label+r'\t[^\n\t]*\t(?:mm|bar)\t(\d+(?:[.,]\d+)?)',text))
        if len(values)==1:technical[field]=Decimal(values.pop().replace(',','.'))
    for field,label in (('mounting','Befestigung'),('certification','Zertifizierung')):
        values=set(re.findall(label+r'\t[^\n\t]*\t[^\n\t]*\t([^\n\t]+)',text))
        if len(values)==1:technical[field]=values.pop().strip()
    groups=set(re.findall(r'Kataloggruppe\s+(GR-\d+)',text))
    if len(groups)==1:technical['catalogue_group']=groups.pop()
    # Certification is also explicitly present in the material description.
    certs=set(re.findall(r'\bEN\s+1493\b',text))
    if certs:technical.setdefault('certification','EN 1493')
    if any(k in technical for k in ('bore','stroke','pressure')):
        fields['specification']=TechnicalRequirement(requirement_id=requirement_id,**technical)
    # Fees need both a label and an explicit number format; unknown is never zero.
    fees=set()
    for source_text in texts:
        locale=re.search(r'Zahlenformat:\s*(en-US|de-DE)',source_text)
        if locale:
            for value in re.findall(r'(?:Rüstkosten|Rüstgebühr|Einrichtungsgebühr)\s*:?\s*([\d.,]+)\s*EUR',source_text):
                fees.add(number(value,locale.group(1)))
    if len(fees)==1:fields['setup_fee']=fees.pop();attributes['setup_fee_currency']='EUR'
    fields['attributes']=attributes
    return fields


def missing_fields(material):
    """Pending fields reflect saved data, accepting either lead-time unit."""
    missing=[]
    for field in ('active','stock','setup_fee','allocation_mass','surcharge_mass','pieces_per_carton'):
        if getattr(material,field) is None:missing.append(field)
    spec=material.specification
    for field in ('catalogue_group','bore','stroke','pressure','mounting','certification'):
        if spec is None or getattr(spec,field) is None:missing.append('specification.'+field)
    for field in ('allocation_mass','surcharge_mass'):
        if not material.attributes.get(field+'_unit'):missing.append(field+'_unit')
    for prefix in ('stock','production'):
        if getattr(material,prefix+'_lead_days') is None and getattr(material,prefix+'_lead_weeks') is None:
            missing.append(prefix+'_lead_time')
    if not material.conditions:missing.append('Price scales could not be parsed with declared number format')
    return missing


def canonical_attributes(material):
    """Remove redundant representations only when their dedicated value exists."""
    attributes=dict(material.attributes)
    for field,aliases in (
        ('allocation_mass',('allocation_mass','mass_allocation')),
        ('surcharge_mass',('surcharge_mass','mass_surcharge')),
        ('pieces_per_carton',('pieces_per_carton','pack_content','packaging')),
        ('setup_fee',('setup_fee',)),
    ):
        if getattr(material,field) is not None:
            for alias in aliases:attributes.pop(alias,None)
    for field in ('allocation_mass','surcharge_mass'):
        if attributes.get(field+'_unit'):
            attributes.pop(field+'_units',None)
    if all(attributes.get(field+'_unit') for field in ('allocation_mass','surcharge_mass')):
        attributes.pop('mass_unit',None)
    return attributes


def clean(text):
    return ''.join(c for c in text if unicodedata.category(c)!='Cf')


def number(text,locale):
    value=text.strip().replace('\u00a0','')
    patterns={'en-US':r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',
              'de-DE':r'[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?'}
    if locale not in patterns or not re.fullmatch(patterns[locale],value):
        raise ValueError('ERP number does not match its declared locale')
    return Decimal(value.replace(',','') if locale=='en-US' else value.replace('.','').replace(',','.'))


def prices(source):
    query=parse_qs(urlsplit(source['url']).query)
    if urlsplit(source['url']).path!='/vk13' or query.get('view')!=['scales']:return []
    text=clean(source['text'])
    locale=re.search(r'Zahlenformat:\s*(en-US|de-DE)',text)
    unit=re.search(r'Preiseinheit:\s*([\d.,]+)\s*ST',text)
    currency=re.search(r'Währung:\s*([A-Z]{3})',text)
    dates=[]
    for label in ('Gültig von','Gültig bis'):
        match=re.search(label+r'\s*(\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}:\d{2})\s*UTC',text)
        if not match:return []
        dates.append(datetime.strptime(match.group(1).replace(',  ', ', '),'%d.%m.%Y, %H:%M:%S').replace(tzinfo=timezone.utc))
    if not locale or not unit or not currency:return []
    result=[]
    for match in re.finditer(r'([\d.,]+)\s*\((en-US|de-DE)\)\s+([\d.,]+)\s*\((en-US|de-DE)\)',text):
        if match.group(2)!=locale.group(1) or match.group(4)!=locale.group(1):
            raise ValueError('Conflicting price-number locales in observed ERP table')
        condition=PriceCondition(price=number(match.group(3),locale.group(1)),currency=currency.group(1),
            price_unit=number(unit.group(1),locale.group(1)),minimum_quantity=number(match.group(1),locale.group(1)),
            valid_from=dates[0],valid_until=dates[1])
        if condition not in result:result.append(condition)
    return result


def available_stock(source):
    query=parse_qs(urlsplit(source['url']).query)
    if urlsplit(source['url']).path!='/mmbe' or query.get('level')!=['stock']:return None
    text=clean(source['text'])
    match=re.search(r'(?:Verfügbarer Bestand|Frei verwendbar(?:er Bestand)?|Verfügbare Menge|Verfügbar)'
                    r'\s*(?:\(ST\))?\s*:?\s*([\d.,]+)',text,re.I)
    if not match:return None
    locale=re.search(r'Zahlenformat:\s*(en-US|de-DE)',text)
    # An integer without separators has the same value in both formats.
    if not locale and not re.fullmatch(r'\d+',match.group(1)):return None
    return number(match.group(1),locale.group(1) if locale else 'en-US')
