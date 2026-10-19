"""Bounded atom/charge accounting; no inference from text/structure similarity.

Reject shorthand, stereochemical notation and missing diagrams instead of
pretending to validate a reaction's products, mechanism or feasibility.
"""
import re
from collections import Counter

# Deliberately limited supported elements. Unknown notation remains unverified.
ELEMENTS = set('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Fe Co Ni Cu Zn Br Ag I Ba Pt Au Hg Pb'.split())


def atoms(formula: str) -> dict[str, int]:
    if not formula or len(formula)>160:
        raise ValueError('Formula size outside bounds')
    tokens=re.findall(r'[A-Z][a-z]?|[1-9][0-9]*|[()]',formula)
    if ''.join(tokens)!=formula:
        raise ValueError('Unsupported formula notation; no guessed structure')
    position=0
    def count():
        nonlocal position
        value=1
        if position<len(tokens) and tokens[position].isdigit():
            value=int(tokens[position]);position+=1
            if value>1000:raise ValueError('Atom count outside bounds')
        return value
    def group(depth=0):
        nonlocal position
        if depth>8:raise ValueError('Formula nesting outside bounds')
        result=Counter()
        while position<len(tokens) and tokens[position]!=')':
            token=tokens[position];position+=1
            if token=='(':
                inner=group(depth+1)
                if position>=len(tokens) or tokens[position]!=')' or not inner:
                    raise ValueError('Invalid parenthesized formula')
                position+=1;factor=count()
                for element,number in inner.items():result[element]+=number*factor
            elif token in ELEMENTS:
                result[token]+=count()
            else:
                raise ValueError('Unknown element or misplaced count')
            if sum(result.values())>100000:raise ValueError('Formula too large')
        return result
    result=group()
    if position!=len(tokens) or not result:
        raise ValueError('Unbalanced parentheses or empty formula')
    return dict(result)


def check_reaction(reaction) -> dict:
    def side(species):
        total=Counter();charge=0
        for item in species:
            for element,number in atoms(item.formula).items():
                total[element]+=number*item.coefficient
            charge+=item.charge*item.coefficient
        return dict(total),charge
    result={'scope':'molecular_formula_atom_and_explicit_charge_accounting',
        'structural_identity_verified':False,'reaction_feasibility_verified':False,
        'mechanism_verified':False,'conditions_verified':False}
    try:
        left,left_charge=side(reaction.reactants)
        right,right_charge=side(reaction.products)
    except ValueError as error:
        return {**result,'status':'UNSUPPORTED','reason':str(error)}
    return {**result,'status':'BALANCED' if left==right and left_charge==right_charge else 'UNBALANCED',
        'reactant_atoms':left,'product_atoms':right,'reactant_charge':left_charge,'product_charge':right_charge}
