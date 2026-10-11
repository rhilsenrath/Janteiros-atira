"""Conservative exact matching for the standalone ERP requirement."""
from core.contracts import MaterialSelection


def select_lead_times(positions,requirements,selections,materials):
    """Pool final demand per material; never allocate stock partially."""
    from decimal import Decimal
    from core.contracts import LeadTimeSelection
    reqs={r.requirement_id:r for r in requirements}
    selected={s.requirement_id:s for s in selections if s.status=='selected'}
    quantities={}
    assignments={}
    errors=[]
    for position in positions:
        selection=selected.get(position.requirement_id)
        requirement=reqs.get(position.requirement_id)
        quantity=position.quantity
        if not selection or not requirement:
            errors.append(f'{position.position_id}: Material selection missing');continue
        assignments[position.position_id]=selection.material_id
        if quantity is None or not quantity.is_finite() or quantity<=0:
            errors.append(f'{position.position_id}: Positive final quantity required');continue
        if position.unit=='Satz':
            factor=requirement.pieces_per_set
            if factor is None or not factor.is_finite() or factor<=0:
                errors.append(f'{position.position_id}: Pieces per set required');continue
            quantity*=factor
        elif position.unit!='ST':
            errors.append(f'{position.position_id}: Unsupported quantity unit');continue
        if quantity!=quantity.to_integral_value():
            errors.append(f'{position.position_id}: Whole pieces required');continue
        quantities[position.position_id]=quantity
    demand={}
    for pid,quantity in quantities.items():
        mid=assignments[pid]
        demand[mid]=demand.get(mid,Decimal(0))+quantity
    results=[]
    for position in positions:
        mid=assignments.get(position.position_id)
        result={'position_id':position.position_id,'material_id':mid}
        # Unresolved positions may share a material: do not undercount demand.
        if errors:
            results.append(LeadTimeSelection(**result,status='incomplete',reasons=errors));continue
        material=next((m for m in materials if m.material_id==mid),None)
        if material is None or material.stock is None or not material.stock.is_finite() or material.stock<0:
            results.append(LeadTimeSelection(**result,status='incomplete',reasons=['Available stock unknown or invalid']));continue
        total=demand[mid]
        basis='stock' if material.stock>=total else 'production'
        result.update(demand_pieces=total,available_stock=material.stock,basis=basis)
        days=getattr(material,basis+'_lead_days')
        weeks=getattr(material,basis+'_lead_weeks')
        if days is not None and weeks is not None and days!=weeks*7:
            results.append(LeadTimeSelection(**result,status='incomplete',reasons=['Conflicting lead-time units']));continue
        duration=weeks if weeks is not None else days
        if duration is None or duration<0:
            results.append(LeadTimeSelection(**result,status='incomplete',reasons=['Applicable lead time unknown or invalid']));continue
        results.append(LeadTimeSelection(**result,status='selected',duration=duration,
            unit='weeks' if weeks is not None else 'days'))
    return results


def select_price(position,requirement,material_selection,materials,scenario_date):
    from core.contracts import PriceSelection
    result={'position_id':position.position_id,'material_id':material_selection.material_id}
    def pending(reason,status='incomplete'):
        return PriceSelection(**result,status=status,reasons=[reason])
    if material_selection.status!='selected':return pending('Material selection is '+material_selection.status)
    material=next((m for m in materials if m.material_id==material_selection.material_id),None)
    if material is None:return pending('Selected material not available')
    if scenario_date is None or scenario_date.utcoffset() is None:return pending('Scenario date with timezone required')
    quantity=position.quantity
    if quantity is None or not quantity.is_finite() or quantity<=0:return pending('Positive final quantity required')
    if position.unit=='Satz':
        factor=requirement.pieces_per_set
        if factor is None or not factor.is_finite() or factor<=0:return pending('Pieces per set required')
        quantity*=factor
    elif position.unit!='ST':return pending('Unsupported or unknown quantity unit')
    if quantity!=quantity.to_integral_value():return pending('Final quantity must be whole pieces')
    result['quantity_pieces']=quantity
    eligible=[]
    for condition in material.conditions:
        if (condition.valid_from is None or condition.valid_until is None or
            condition.valid_from.utcoffset() is None or condition.valid_until.utcoffset() is None):
            return pending('Condition validity is unknown')
        if condition.valid_from>condition.valid_until:return pending('Invalid condition validity interval')
        if not condition.valid_from<=scenario_date<=condition.valid_until:continue
        minimum=condition.minimum_quantity
        if minimum is None or not minimum.is_finite() or minimum<0:return pending('Quantity scale is unknown or invalid')
        if minimum>quantity:continue
        if (condition.price is None or not condition.price.is_finite() or condition.price<0 or
            condition.price_unit is None or not condition.price_unit.is_finite() or condition.price_unit<=0 or
            not condition.currency):return pending('Price, currency or price unit is unknown or invalid')
        eligible.append(condition)
    if not eligible:return pending('No valid price scale reached on scenario date','no_match')
    maximum=max(c.minimum_quantity for c in eligible)
    top=[c for c in eligible if c.minimum_quantity==maximum]
    unique={c.model_dump_json(exclude={'evidence'}):c for c in top}
    if len(unique)!=1:return pending('Conflicting conditions at the highest applicable scale','ambiguous')
    condition=next(iter(unique.values()))
    return PriceSelection(**result,status='selected',condition=condition,
        base_price_per_piece=condition.price/condition.price_unit)


def select_material(requirement,materials,*,complete=True):
    fields=('bore','stroke','pressure','mounting','certification')
    if requirement.stroke_kind not in (None,'nominal'):
        return MaterialSelection(requirement_id=requirement.requirement_id,status='incomplete',
            reasons={'requirement':['Nominal stroke must be supplied before ERP matching']})
    absent=[field for field in fields if getattr(requirement,field) is None]
    if absent:
        return MaterialSelection(requirement_id=requirement.requirement_id,status='incomplete',
            reasons={'requirement':['Missing '+field for field in absent]})
    reasons={}
    eligible=[]
    uncertain=False
    for material in materials:
        rejected=[]
        unknown=[]
        if material.active is False:rejected.append('Material is blocked')
        elif material.active is None:unknown.append('Active status is unknown')
        for field in fields:
            value=getattr(material.specification,field) if material.specification else None
            if value is None:unknown.append('Missing '+field)
            elif value!=getattr(requirement,field):rejected.append('Mismatch: '+field)
        reasons[material.material_id]=rejected+unknown
        if not rejected:
            if unknown:uncertain=True
            else:eligible.append(material.material_id)
    if not complete or uncertain:status='incomplete'
    elif len(eligible)>1:status='ambiguous'
    elif eligible:status='selected'
    else:status='no_match'
    return MaterialSelection(requirement_id=requirement.requirement_id,status=status,
        material_id=eligible[0] if status=='selected' else None,reasons=reasons)
