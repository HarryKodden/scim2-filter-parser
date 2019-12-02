"""
The logic in this module builds a Django Q object from an SCIM filter.
"""
import ast
from typing import Mapping

from django.db.models import Q

from scim2_filter_parser import ast as scim2ast
from scim2_filter_parser.lexer import SCIMLexer
from scim2_filter_parser.parser import SCIMParser


def get_query(scim_query: str, attr_map: Mapping):
    token_stream = SCIMLexer().tokenize(scim_query)
    tree = SCIMParser().parse(token_stream)
    return Transpiler(attr_map).transpile(tree)


# noinspection PyPep8Naming
class Transpiler(ast.NodeTransformer):
    """
    Transpile a SCIM AST into a Q object
    """

    def __init__(self, attr_map: Mapping, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attr_map = attr_map

    def transpile(self, scim_ast) -> (str, dict):
        return self.visit(scim_ast)

    def visit_Filter(self, node):
        if node.namespace:
            # push the namespace from value path down the tree
            if isinstance(node.expr, scim2ast.Filter):
                node.expr = scim2ast.Filter(
                    node.expr.expr, node.expr.negated, node.namespace
                )
            elif isinstance(node.expr, scim2ast.LogExpr):
                expr1 = scim2ast.Filter(
                    node.expr.expr1.expr, node.expr.expr1.negated, node.namespace
                )
                expr2 = scim2ast.Filter(
                    node.expr.expr2.expr, node.expr.expr2.negated, node.namespace
                )
                node.expr = scim2ast.LogExpr(node.expr.op, expr1, expr2)
            elif isinstance(node.expr, scim2ast.AttrExpr):
                # namespace takes place of previous attr_name in attr_path
                sub_attr = scim2ast.SubAttr(node.expr.attr_path.attr_name)
                attr_path = scim2ast.AttrPath(
                    node.namespace.attr_name, sub_attr, node.expr.attr_path.uri
                )
                node.expr = scim2ast.AttrExpr(
                    node.expr.value, attr_path, node.expr.comp_value
                )
            else:
                raise NotImplementedError(f"Node {node} can not pass on namespace")

        query = self.visit(node.expr)

        if query and node.negated:
            query = ~query

        return query

    def visit_LogExpr(self, node):
        q1 = self.visit(node.expr1)
        q2 = self.visit(node.expr2)
        op = node.op.upper()
        if q1 and q2:
            if op == "AND":
                return q1 & q2
            elif op == "OR":
                return q1 | q2
        elif q1:
            return q1
        elif q2:
            return q2
        else:
            return None

    def visit_PartialAttrExpr(self, node):
        """
        Dissect rather complex queries like the following::

            emails[type eq "Primary"].value eq "001750ca-8202-47cd-b553-c63f4f245940"

        First we restructure to something like this::

            emails.value[type eq "Primary"] eq "001750ca-8202-47cd-b553-c63f4f245940"

        Then we get SQL like this 'emails.type = {0}' and 'emails.value'.

        We need to take these two snippets and AND them together.
        """
        # visit full filter first and restructure AST
        # ie. visit -> 'emails.type = {0}'
        full = self.visit(node)

        # get second part of query
        # ie. visit -> 'emails.value'
        partial = self.visit(node.namespace)

        return full, partial

    def is_filter(self, node):
        full, partial = self.visit_PartialAttrExpr(node.attr_path.attr_name)
        if partial and "." in partial:
            partial = partial.replace(".", "__")
        if full and partial:
            # Specific to Azure
            op, value = self.visit_AttrExprValue(node.value, node.comp_value)
            key = partial + "__" + op
            return full & Q(**{key: value})
        elif full:
            return full
        elif partial:
            op, value = self.visit_AttrExprValue(node.value, node.comp_value)
            key = partial + "__" + op
            return Q(**{key: value})
        else:
            return None

    def visit_AttrExpr(self, node):
        if isinstance(node.attr_path.attr_name, scim2ast.Filter):
            return self.is_filter(node)
        attr = self.visit(node.attr_path)
        if attr is None:
            return None
        if "." in attr:
            attr = attr.replace(".", "__")
        op, value = self.visit_AttrExprValue(node.value, node.comp_value)
        key = attr + "__" + op
        query = Q(**{key: value})
        if node.value == "ne":
            query = ~query
        return query

    def visit_AttrExprValue(self, node_value, node_comp_value):
        op = self.lookup_op(node_value)

        if node_comp_value:
            # There is a comp_value, so visit node and build SQL.
            # prep item_id to be a str replacement placeholder
            value = self.visit(node_comp_value)
        else:
            value = None

        if op == "isnull":  # __isnull=False
            value = False

        return op, value

    def visit_AttrPath(self, node):
        attr_name_value = node.attr_name

        sub_attr_value = None
        if node.sub_attr:
            sub_attr_value = node.sub_attr.value

        uri_value = None
        if node.uri:
            uri_value = node.uri

        # Convert attr_name to another value based on map.
        # Otherwise, return None.
        attr_path_tuple = (attr_name_value, sub_attr_value, uri_value)
        return self.attr_map.get(attr_path_tuple)

    @staticmethod
    def visit_CompValue(node):
        if node.value == "true":
            return True
        elif node.value == "false":
            return False
        elif node.value == "null":
            return None
        else:
            return node.value

    @staticmethod
    def lookup_op(node_value):
        op_code = node_value.lower()

        op = {
            "eq": "iexact",
            "ne": "iexact",
            "co": "icontains",
            "sw": "istartswith",
            "ew": "iendswith",
            "pr": "isnull",
            "gt": "gt",
            "ge": "gte",
            "lt": "lt",
            "le": "lte",
        }.get(op_code)

        if not op:
            raise ValueError(f"Unknown Django op {op_code}")

        return op or node_value
