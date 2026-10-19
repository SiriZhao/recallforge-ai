"""Small, bounded arithmetic interpreter. Never eval/exec uploaded model code."""
import ast
import math
import operator


def calculate(expression):
    if not isinstance(expression,str) or len(expression)>240:
        raise ValueError("Expression too large")
    tree=ast.parse(expression,mode='eval')
    if len(list(ast.walk(tree)))>80:
        raise ValueError("Expression too complex")
    operations={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Mod:operator.mod}
    def visit(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):
            value=node.value
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=visit(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp):
            left,right=visit(node.left),visit(node.right)
            if isinstance(node.op,ast.Pow):
                if abs(right)>12 or abs(left)>1e6:raise ValueError("Power outside bounds")
                value=left**right
            elif type(node.op) in operations:value=operations[type(node.op)](left,right)
            else:raise ValueError("Operator not allowed")
        elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and not node.keywords:
            args=[visit(arg) for arg in node.args]
            if node.func.id=='sqrt' and len(args)==1:value=math.sqrt(args[0])
            elif node.func.id=='comb' and len(args)==2 and all(type(arg)==int and 0<=arg<=200 for arg in args):value=math.comb(*args)
            elif node.func.id=='factorial' and len(args)==1 and type(args[0])==int and 0<=args[0]<=100:value=math.factorial(args[0])
            else:raise ValueError("Function or arguments not allowed")
        else:raise ValueError("Expression contains unsafe or unsupported syntax")
        if not isinstance(value,(int,float)) or not math.isfinite(value) or abs(value)>1e100:
            raise ValueError("Nonfinite or oversized result")
        return value
    return visit(tree.body)


def verify(expression,expected,quantity_kind="number",tolerance=1e-8):
    try:
        computed=calculate(expression)
        expected=float(expected)
        if not math.isfinite(expected):return False
        if quantity_kind=='probability' and not 0<=computed<=1:return False
        return math.isclose(computed,expected,rel_tol=tolerance,abs_tol=tolerance)
    except (ValueError,TypeError,SyntaxError,ZeroDivisionError,OverflowError):
        return False
