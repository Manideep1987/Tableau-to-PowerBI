"""Tokenizer and recursive-descent parser for the Tableau calculation language."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import (Bool, BinOp, Case, DateLit, Field, Func, If, In, LOD, Null, Num,
                  Str, Unary, Node)


class FormulaError(ValueError):
    pass


@dataclass
class Tok:
    kind: str      # NUM STR FIELD DATE IDENT OP EOF
    value: str
    pos: int


KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR", "NOT",
            "IN", "TRUE", "FALSE", "NULL", "FIXED", "INCLUDE", "EXCLUDE"}

_OPS = ["<=", ">=", "<>", "!=", "==", "&&", "||", "(", ")", ",", "{", "}", ":", "+", "-",
        "*", "/", "%", "^", "=", "<", ">", "."]


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise FormulaError("Unterminated block comment")
            i = j + 2
            continue
        if c == "[":
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "]":
                    if j + 1 < n and text[j + 1] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            if j >= n:
                raise FormulaError(f"Unterminated field reference at {i}")
            toks.append(Tok("FIELD", "".join(buf), i))
            i = j + 1
            continue
        if c in "'\"":
            q = c
            j = i + 1
            buf = []
            while j < n:
                if text[j] == q:
                    if j + 1 < n and text[j + 1] == q:
                        buf.append(q)
                        j += 2
                        continue
                    break
                if text[j] == "\\" and j + 1 < n and text[j + 1] == q:
                    buf.append(q)
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise FormulaError(f"Unterminated string at {i}")
            toks.append(Tok("STR", "".join(buf), i))
            i = j + 1
            continue
        if c == "#":
            j = text.find("#", i + 1)
            if j < 0:
                raise FormulaError("Unterminated date literal")
            toks.append(Tok("DATE", text[i + 1:j].strip(), i))
            i = j + 1
            continue
        m = re.match(r"\d+\.?\d*([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?", text[i:])
        if m:
            toks.append(Tok("NUM", m.group(0), i))
            i += len(m.group(0))
            continue
        m = re.match(r"[A-Za-z_][A-Za-z_0-9\.]*", text[i:])
        if m:
            word = m.group(0)
            toks.append(Tok("IDENT", word, i))
            i += len(word)
            continue
        for op in _OPS:
            if text.startswith(op, i):
                toks.append(Tok("OP", op, i))
                i += len(op)
                break
        else:
            raise FormulaError(f"Unexpected character {c!r} at {i}")
    toks.append(Tok("EOF", "", n))
    return toks


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    # -- helpers -----------------------------------------------------------
    def peek(self, k=0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_kw(self, word, k=0) -> bool:
        t = self.peek(k)
        return t.kind == "IDENT" and t.value.upper() == word

    def is_op(self, op, k=0) -> bool:
        t = self.peek(k)
        return t.kind == "OP" and t.value == op

    def expect_kw(self, word):
        if not self.is_kw(word):
            raise FormulaError(f"Expected {word} at {self.peek().pos}, got {self.peek().value!r}")
        return self.next()

    def expect_op(self, op):
        if not self.is_op(op):
            raise FormulaError(f"Expected '{op}' at {self.peek().pos}, got {self.peek().value!r}")
        return self.next()

    # -- grammar -----------------------------------------------------------
    def parse(self) -> Node:
        node = self.expr()
        if self.peek().kind != "EOF":
            raise FormulaError(f"Unexpected token {self.peek().value!r} at {self.peek().pos}")
        return node

    def expr(self):
        return self.or_()

    def or_(self):
        left = self.and_()
        while self.is_kw("OR") or self.is_op("||"):
            self.next()
            left = BinOp("OR", left, self.and_())
        return left

    def and_(self):
        left = self.not_()
        while self.is_kw("AND") or self.is_op("&&"):
            self.next()
            left = BinOp("AND", left, self.not_())
        return left

    def not_(self):
        if self.is_kw("NOT"):
            self.next()
            return Unary("NOT", self.not_())
        return self.comparison()

    def comparison(self):
        left = self.additive()
        while True:
            t = self.peek()
            if t.kind == "OP" and t.value in ("=", "==", "<>", "!=", "<", ">", "<=", ">="):
                self.next()
                op = {"==": "=", "!=": "<>"}.get(t.value, t.value)
                left = BinOp(op, left, self.additive())
            elif self.is_kw("IN") or (self.is_kw("NOT") and self.is_kw("IN", 1)):
                neg = False
                if self.is_kw("NOT"):
                    self.next()
                    neg = True
                self.next()
                self.expect_op("(")
                vals = [self.expr()]
                while self.is_op(","):
                    self.next()
                    vals.append(self.expr())
                self.expect_op(")")
                left = In(left, vals, neg)
            else:
                return left

    def additive(self):
        left = self.multiplicative()
        while self.is_op("+") or self.is_op("-"):
            op = self.next().value
            left = BinOp(op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.power()
        while self.is_op("*") or self.is_op("/") or self.is_op("%"):
            op = self.next().value
            left = BinOp(op, left, self.power())
        return left

    def power(self):
        left = self.unary()
        if self.is_op("^"):
            self.next()
            return BinOp("^", left, self.power())
        return left

    def unary(self):
        if self.is_op("-"):
            self.next()
            operand = self.unary()
            if isinstance(operand, Num):
                return Num(-operand.value)
            return Unary("-", operand)
        if self.is_op("+"):
            self.next()
            return self.unary()
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "NUM":
            self.next()
            v = float(t.value)
            return Num(int(v) if v.is_integer() and "." not in t.value and "e" not in t.value.lower() else v)
        if t.kind == "STR":
            self.next()
            return Str(t.value)
        if t.kind == "DATE":
            self.next()
            return DateLit(t.value)
        if t.kind == "FIELD":
            self.next()
            # [Datasource].[Field]
            if self.is_op(".") and self.peek(1).kind == "FIELD":
                self.next()
                f2 = self.next()
                return Field(f2.value, t.value)
            return Field(t.value)
        if t.kind == "OP" and t.value == "(":
            self.next()
            e = self.expr()
            self.expect_op(")")
            return e
        if t.kind == "OP" and t.value == "{":
            return self.lod()
        if t.kind == "IDENT":
            word = t.value.upper()
            if word == "IF":
                return self.if_()
            if word == "CASE":
                return self.case()
            if word == "TRUE":
                self.next()
                return Bool(True)
            if word == "FALSE":
                self.next()
                return Bool(False)
            if word == "NULL":
                self.next()
                return Null()
            # function call
            self.next()
            if not self.is_op("("):
                raise FormulaError(f"Unknown identifier {t.value!r} at {t.pos}")
            self.next()
            args = []
            if not self.is_op(")"):
                args.append(self.expr())
                while self.is_op(","):
                    self.next()
                    args.append(self.expr())
            self.expect_op(")")
            return Func(word, args)
        raise FormulaError(f"Unexpected token {t.value!r} at {t.pos}")

    def if_(self):
        self.expect_kw("IF")
        branches = []
        cond = self.expr()
        self.expect_kw("THEN")
        branches.append((cond, self.expr()))
        else_ = None
        while True:
            if self.is_kw("ELSEIF"):
                self.next()
                c = self.expr()
                self.expect_kw("THEN")
                branches.append((c, self.expr()))
            elif self.is_kw("ELSE"):
                self.next()
                if self.is_kw("IF"):     # "ELSE IF" written as two words
                    nested = self.if_()
                    else_ = nested
                    break
                else_ = self.expr()
            else:
                break
        self.expect_kw("END")
        return If(branches, else_)

    def case(self):
        self.expect_kw("CASE")
        subject = self.expr()
        whens = []
        while self.is_kw("WHEN"):
            self.next()
            m = self.expr()
            self.expect_kw("THEN")
            whens.append((m, self.expr()))
        else_ = None
        if self.is_kw("ELSE"):
            self.next()
            else_ = self.expr()
        self.expect_kw("END")
        return Case(subject, whens, else_)

    def lod(self):
        self.expect_op("{")
        kind = "FIXED"
        dims = []
        if self.is_kw("FIXED") or self.is_kw("INCLUDE") or self.is_kw("EXCLUDE"):
            kind = self.next().value.upper()
            if not self.is_op(":"):
                dims.append(self.expr())
                while self.is_op(","):
                    self.next()
                    dims.append(self.expr())
            self.expect_op(":")
        expr = self.expr()
        self.expect_op("}")
        return LOD(kind, dims, expr)


def parse_formula(text: str) -> Node:
    return Parser(text).parse()
