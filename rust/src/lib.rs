//! Native decoder for the WeCom online-sheet `related_sheet` payload.
//!
//! A port of `wecom_excel_mcp/decoder.py` (see its module docstring for the field layout). It
//! returns plain Python values; `decoder.py` wraps them into `SheetData`. Both implementations
//! must agree exactly: the Python one is the reference and the tests run against both.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

const MERGE_BLOCK_TYPE: i64 = 5;
const SIZE_BLOCK_TYPE: i64 = 13;
const CELL_BLOCK_TYPE: i64 = 18;
const CELL_KIND_PICTURE: u64 = 5;
const TYPE_NUMBER: i64 = 2;
const TYPE_STRING: i64 = 4;
const TYPE_FORMULA: i64 = 5;
const TYPE_RICH_STRING: i64 = 6;
const INLINE_INT_LIMIT: i64 = 129;
const NUMBER_TABLE_OFFSET: i64 = 129;

const VARINT: u8 = 0;
const FIXED64: u8 = 1;
const LENDELIM: u8 = 2;
const FIXED32: u8 = 5;

type Span = (usize, usize);
type Res<T> = Result<T, String>;

// ---------------------------------------------------------------- protobuf walking

#[derive(Clone, Copy)]
enum Val {
    Varint(u64),
    Span(Span),
    Fixed64([u8; 8]),
    Fixed32,
}

fn read_varint(buf: &[u8], mut pos: usize) -> Res<(u64, usize)> {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    loop {
        let Some(&b) = buf.get(pos) else { return Err("truncated varint".into()) };
        pos += 1;
        result |= ((b & 0x7F) as u64) << shift;
        if b & 0x80 == 0 {
            return Ok((result, pos));
        }
        shift += 7;
        if shift > 63 {
            return Err("varint too long".into());
        }
    }
}

struct Fields<'a> {
    buf: &'a [u8],
    pos: usize,
    end: usize,
}

impl<'a> Iterator for Fields<'a> {
    type Item = Res<(u64, Val)>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.pos >= self.end {
            return None;
        }
        Some(self.step())
    }
}

impl<'a> Fields<'a> {
    fn step(&mut self) -> Res<(u64, Val)> {
        let (key, pos) = read_varint(self.buf, self.pos)?;
        self.pos = pos;
        let (fno, wt) = (key >> 3, (key & 7) as u8);
        let val = match wt {
            VARINT => {
                let (v, pos) = read_varint(self.buf, self.pos)?;
                self.pos = pos;
                Val::Varint(v)
            }
            LENDELIM => {
                let (ln, pos) = read_varint(self.buf, self.pos)?;
                let end = pos.checked_add(ln as usize).filter(|&e| e <= self.end && ln <= usize::MAX as u64);
                let Some(end) = end else { return Err("length-delimited field overruns buffer".into()) };
                self.pos = end;
                Val::Span((pos, end))
            }
            FIXED64 => {
                if self.pos + 8 > self.end {
                    return Err("truncated fixed-width field".into());
                }
                let mut b = [0u8; 8];
                b.copy_from_slice(&self.buf[self.pos..self.pos + 8]);
                self.pos += 8;
                Val::Fixed64(b)
            }
            FIXED32 => {
                if self.pos + 4 > self.end {
                    return Err("truncated fixed-width field".into());
                }
                self.pos += 4;
                Val::Fixed32
            }
            _ => return Err(format!("unsupported wire type {wt}")),
        };
        Ok((fno, val))
    }
}

fn fields(buf: &[u8], span: Span) -> Fields<'_> {
    Fields { buf, pos: span.0, end: span.1 }
}

/// Sub-spans of field `fno` inside message `span`.
fn subs(buf: &[u8], span: Span, fno: u64) -> impl Iterator<Item = Res<Span>> + '_ {
    fields(buf, span).filter_map(move |f| match f {
        Ok((g, Val::Span(s))) if g == fno => Some(Ok(s)),
        Ok(_) => None,
        Err(e) => Some(Err(e)),
    })
}

fn first_sub(buf: &[u8], span: Span, fno: u64) -> Res<Option<Span>> {
    for f in fields(buf, span) {
        if let (g, Val::Span(s)) = f? {
            if g == fno {
                return Ok(Some(s));
            }
        }
    }
    Ok(None)
}

fn text(buf: &[u8], s: Span) -> String {
    String::from_utf8_lossy(&buf[s.0..s.1]).into_owned()
}

fn push_text(out: &mut String, buf: &[u8], s: Span) {
    out.push_str(&String::from_utf8_lossy(&buf[s.0..s.1]));
}

/// Value of varint field `fno` inside the message at `span`, as a signed 64-bit int.
fn int_in(buf: &[u8], span: Span, fno: u64) -> Res<Option<i64>> {
    for f in fields(buf, span) {
        if let (g, Val::Varint(v)) = f? {
            if g == fno {
                return Ok(Some(v as i64));
            }
        }
    }
    Ok(None)
}

fn int_or0(buf: &[u8], span: Span) -> Res<i64> {
    Ok(int_in(buf, span, 1)?.unwrap_or(0))
}

fn double_in(buf: &[u8], span: Span) -> Res<Option<f64>> {
    for f in fields(buf, span) {
        if let (1, Val::Fixed64(b)) = f? {
            return Ok(Some(f64::from_le_bytes(b)));
        }
    }
    Ok(None)
}

fn col_letter(idx: i128) -> String {
    let mut s = Vec::new();
    let mut idx = idx + 1;
    while idx > 0 {
        let r = (idx - 1) % 26;
        idx = (idx - 1) / 26;
        s.push(b'A' + r as u8);
    }
    s.reverse();
    String::from_utf8(s).unwrap()
}

// ---------------------------------------------------------------- styles

#[derive(Clone, Default, PartialEq, Eq, Hash)]
struct Style {
    bold: bool,
    color: Option<String>,
    size: Option<i64>,
    font: Option<String>,
    fill: Option<String>,
    h_align: Option<&'static str>,
    v_align: Option<&'static str>,
    wrap: bool,
    border: bool,
}

#[derive(Clone, Default, PartialEq, Eq, Hash)]
struct Font {
    bold: bool,
    color: Option<String>,
    size: Option<i64>,
    font: Option<String>,
}

impl Font {
    fn style(&self) -> Style {
        Style {
            bold: self.bold,
            color: self.color.clone(),
            size: self.size,
            font: self.font.clone(),
            ..Style::default()
        }
    }
}

/// All distinct styles of a decode, so Python builds each `Style` once.
#[derive(Default)]
struct Interner {
    list: Vec<Style>,
    ids: HashMap<Style, usize>,
}

impl Interner {
    fn id(&mut self, s: Style) -> usize {
        if let Some(&i) = self.ids.get(&s) {
            return i;
        }
        let i = self.list.len();
        self.list.push(s.clone());
        self.ids.insert(s, i);
        i
    }
}

fn builtin_date_fmt(id: i64) -> Option<&'static str> {
    match id {
        14..=17 => Some("date"),
        22 => Some("datetime"),
        18..=21 | 45..=47 => Some("time"),
        _ => None,
    }
}

fn h_align(v: i64) -> Option<&'static str> {
    match v {
        2 => Some("left"),
        3 => Some("center"),
        4 => Some("right"),
        _ => None,
    }
}

fn v_align(v: i64) -> Option<&'static str> {
    match v {
        1 => Some("top"),
        2 => Some("middle"),
        3 => Some("bottom"),
        _ => None,
    }
}

/// Same as decoder.py `_date_kind`: strip quoted text, [..] sections and \-escapes, then look
/// for date (y/d) and time (h/s) letters in the first section.
fn date_kind(code: &str) -> Option<&'static str> {
    let first = code.split(';').next().unwrap_or("");
    let chars: Vec<char> = first.chars().collect();
    let mut kept = String::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        let close = |ch: char| chars[i + 1..].iter().position(|&x| x == ch);
        if c == '"' {
            if let Some(j) = close('"') {
                i += j + 2;
                continue;
            }
        } else if c == '[' {
            if let Some(j) = close(']') {
                i += j + 2;
                continue;
            }
        } else if c == '\\' && i + 1 < chars.len() {
            i += 2;
            continue;
        }
        kept.push(c);
        i += 1;
    }
    let kept = kept.to_lowercase();
    let has_date = kept.contains('y') || kept.contains('d');
    let has_time = kept.contains('h') || kept.contains('s');
    match (has_date, has_time) {
        (true, true) => Some("datetime"),
        (true, false) => Some("date"),
        (false, true) => Some("time"),
        _ => None,
    }
}

/// `{ 5 { 1: "AARRGGBB" } }` -> "#RRGGBB".
fn argb(buf: &[u8], span: Span) -> Res<Option<String>> {
    for c in subs(buf, span, 5) {
        let c = c?;
        for s in subs(buf, c, 1) {
            let s = s?;
            let a = text(buf, s);
            let n = a.chars().count();
            if n == 6 || n == 8 {
                let tail: String = a.chars().skip(n - 6).collect();
                return Ok(Some(format!("#{}", tail.to_uppercase())));
            }
        }
    }
    Ok(None)
}

fn read_font(buf: &[u8], span: Span) -> Res<Font> {
    let mut font = Font::default();
    for f in fields(buf, span) {
        let (g, v) = f?;
        let Val::Span(v) = v else { continue };
        match g {
            1 => {
                let name = match first_sub(buf, v, 1)? {
                    Some(s) => text(buf, s),
                    None => String::new(),
                };
                font.font = if name.is_empty() { None } else { Some(name) };
            }
            7 => font.bold = int_or0(buf, v)? != 0,
            21 => font.color = argb(buf, v)?,
            23 => font.size = Some(int_or0(buf, v)?).filter(|&n| n != 0),
            _ => {}
        }
    }
    Ok(font)
}

fn read_fill(buf: &[u8], span: Span) -> Res<Option<String>> {
    for pattern in subs(buf, span, 1) {
        let pattern = pattern?;
        if int_in(buf, pattern, 5)?.unwrap_or(0) == 2 {
            // solid; 1 = none (a colour may still be recorded)
            return match first_sub(buf, pattern, 1)? {
                Some(c) => argb(buf, c),
                None => Ok(None),
            };
        }
    }
    Ok(None)
}

fn read_border(buf: &[u8], span: Span) -> Res<bool> {
    for f in fields(buf, span) {
        if let (5 | 7 | 9 | 11, Val::Span(side)) = f? {
            if int_in(buf, side, 3)?.unwrap_or(0) != 0 {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Per style index: date kind and interned style id (None = plain).
fn read_styles(buf: &[u8], span: Span, t: &mut Tables, styles: &mut Interner) -> Res<()> {
    let mut custom: HashMap<i64, String> = HashMap::new();
    let mut xfs: Vec<HashMap<u64, Span>> = Vec::new();
    let mut fonts: Vec<Font> = Vec::new();
    let mut fills: Vec<Option<String>> = Vec::new();
    let mut borders: Vec<bool> = Vec::new();
    for f in fields(buf, span) {
        let (g, v) = f?;
        let Val::Span(v) = v else { continue };
        match g {
            1 => {
                let mut xf = HashMap::new();
                for h in fields(buf, v) {
                    if let (n, Val::Span(s)) = h? {
                        xf.entry(n).or_insert(s);
                    }
                }
                xfs.push(xf);
            }
            2 => {
                let fid = match first_sub(buf, v, 1)? {
                    Some(i) => int_or0(buf, i)?,
                    None => 0,
                };
                for code in subs(buf, v, 3) {
                    let code = code?;
                    for s in subs(buf, code, 1) {
                        let s = s?;
                        custom.insert(fid, text(buf, s));
                    }
                }
            }
            3 => fonts.push(read_font(buf, v)?),
            4 => fills.push(read_fill(buf, v)?),
            5 => borders.push(read_border(buf, v)?),
            _ => {}
        }
    }

    fn pick<T: Clone>(buf: &[u8], xf: &HashMap<u64, Span>, fno: u64, table: &[T], default: T) -> Res<T> {
        let Some(&s) = xf.get(&fno) else { return Ok(default) };
        let i = int_or0(buf, s)?;
        Ok(if i >= 0 && (i as usize) < table.len() { table[i as usize].clone() } else { default })
    }

    t.style_kinds.clear();
    t.style_ids.clear();
    for xf in &xfs {
        let fmt_id = match xf.get(&7) {
            Some(&s) => int_or0(buf, s)?,
            None => 0,
        };
        t.style_kinds.push(match custom.get(&fmt_id) {
            Some(code) => date_kind(code),
            None => builtin_date_fmt(fmt_id),
        });
        let font = pick(buf, xf, 9, &fonts, Font::default())?;
        let mut st = font.style();
        st.fill = pick(buf, xf, 11, &fills, None)?;
        st.border = pick(buf, xf, 13, &borders, false)?;
        if let Some(&al) = xf.get(&1) {
            st.h_align = h_align(int_in(buf, al, 1)?.unwrap_or(0));
            st.v_align = v_align(int_in(buf, al, 3)?.unwrap_or(0));
            for w in subs(buf, al, 7) {
                let w = w?;
                if int_or0(buf, w)? != 0 {
                    st.wrap = true;
                    break;
                }
            }
        }
        t.style_ids.push(if st == Style::default() { None } else { Some(styles.id(st)) });
    }
    Ok(())
}

// ---------------------------------------------------------------- value tables

#[derive(Clone)]
struct Ref {
    v: [i64; 8], // fields 1, 3, 5, 7, 13, 15, 17, 19
    sheet: Option<String>,
}

enum Piece {
    Text(String),
    Ref(Ref),
}

#[derive(Default)]
struct Tables {
    plain: Vec<String>,
    rich: Vec<String>,
    rich_runs: Vec<Option<Vec<(String, usize)>>>, // None: one uniform format
    numbers: Vec<f64>,
    formulas: Vec<(Option<i64>, i64, i64)>, // (cached type, cached idx, template)
    templates: Vec<Vec<Piece>>,
    style_kinds: Vec<Option<&'static str>>,
    style_ids: Vec<Option<usize>>,
}

fn read_ref(buf: &[u8], span: Span) -> Res<Ref> {
    let mut r = Ref { v: [0; 8], sheet: None };
    for f in fields(buf, span) {
        let (g, v) = f?;
        let Val::Span(v) = v else { continue };
        let slot = match g {
            1 => 0,
            3 => 1,
            5 => 2,
            7 => 3,
            13 => 4,
            15 => 5,
            17 => 6,
            19 => 7,
            21 => {
                r.sheet = first_sub(buf, v, 1)?.map(|s| text(buf, s));
                continue;
            }
            _ => continue,
        };
        r.v[slot] = int_or0(buf, v)?;
    }
    Ok(r)
}

fn read_value_tables(buf: &[u8], span: Span, t: &mut Tables, styles: &mut Interner) -> Res<()> {
    for f in fields(buf, span) {
        let (g, v) = f?;
        let Val::Span(v) = v else { continue };
        match g {
            1 => {
                let mut s = String::new();
                for p in subs(buf, v, 1) {
                    let p = p?;
                    push_text(&mut s, buf, p);
                }
                t.plain.push(s);
            }
            2 => {
                let mut runs: Vec<(String, Font)> = Vec::new();
                for run in subs(buf, v, 3) {
                    let run = run?;
                    let mut s = String::new();
                    for inner in subs(buf, run, 3) {
                        let inner = inner?;
                        for p in subs(buf, inner, 1) {
                            let p = p?;
                            push_text(&mut s, buf, p);
                        }
                    }
                    let mut font = None;
                    'outer: for fmt in subs(buf, run, 1) {
                        let fmt = fmt?;
                        if let Some(fs) = first_sub(buf, fmt, 1)? {
                            font = Some(read_font(buf, fs)?);
                            break 'outer;
                        }
                    }
                    runs.push((s, font.unwrap_or_default()));
                }
                t.rich.push(runs.iter().map(|(s, _)| s.as_str()).collect());
                let mixed = runs.iter().any(|(_, f)| *f != runs[0].1);
                t.rich_runs.push(if mixed {
                    Some(runs.into_iter().map(|(s, f)| (s, styles.id(f.style()))).collect())
                } else {
                    None
                });
            }
            3 => {
                let mut num = 0.0;
                for h in fields(buf, v) {
                    if let (1, Val::Fixed64(b)) = h? {
                        num = f64::from_le_bytes(b);
                    }
                }
                t.numbers.push(num);
            }
            4 => {
                let (mut ctype, mut cidx, mut tmpl) = (None, 0, 0);
                for h in fields(buf, v) {
                    match h? {
                        (1, Val::Span(gv)) => {
                            ctype = int_in(buf, gv, 1)?;
                            for idx_span in subs(buf, gv, 3) {
                                let idx_span = idx_span?;
                                cidx = int_or0(buf, idx_span)?;
                            }
                        }
                        (3, Val::Span(gv)) => tmpl = int_or0(buf, gv)?,
                        _ => {}
                    }
                }
                t.formulas.push((ctype, cidx, tmpl));
            }
            5 => {
                let mut pieces = Vec::new();
                for body in subs(buf, v, 1) {
                    let body = body?;
                    for piece in subs(buf, body, 1) {
                        let piece = piece?;
                        for h in fields(buf, piece) {
                            match h? {
                                (1, Val::Span(gv)) => {
                                    for s in subs(buf, gv, 1) {
                                        let s = s?;
                                        pieces.push(Piece::Text(text(buf, s)));
                                    }
                                }
                                (3, Val::Span(gv)) => pieces.push(Piece::Ref(read_ref(buf, gv)?)),
                                _ => {}
                            }
                        }
                    }
                }
                t.templates.push(pieces);
            }
            _ => {}
        }
    }
    Ok(())
}

// ---------------------------------------------------------------- formulas

fn is_plain_sheet_name(name: &str) -> bool {
    let b = name.as_bytes();
    let ident = !b.is_empty()
        && (b[0].is_ascii_alphabetic() || b[0] == b'_')
        && b[1..].iter().all(|&c| c.is_ascii_alphanumeric() || c == b'_' || c == b'.');
    // A name like "AB12" reads as a cell address, so it must be quoted.
    let letters = b.iter().take_while(|c| c.is_ascii_alphabetic()).count();
    let looks_like_cell =
        (1..=3).contains(&letters) && b.len() > letters && b[letters..].iter().all(|c| c.is_ascii_digit());
    ident && !looks_like_cell
}

fn sheet_prefix(name: &str) -> String {
    if is_plain_sheet_name(name) {
        format!("{name}!")
    } else {
        format!("'{}'!", name.replace('\'', "''"))
    }
}

/// Addresses are computed in i128 so no field value can overflow (Python ints are unbounded).
fn render_formula(pieces: &[Piece], row: i128, col: i128, sheet: &str, names: &HashMap<String, String>) -> String {
    let mut out = String::new();
    for p in pieces {
        let p = match p {
            Piece::Text(s) => {
                out.push_str(s);
                continue;
            }
            Piece::Ref(r) => r,
        };
        if let Some(rs) = p.sheet.as_deref().filter(|s| !s.is_empty() && *s != sheet) {
            out.push_str(&sheet_prefix(names.get(rs).map(String::as_str).unwrap_or(rs)));
        }
        let addr = |rk: usize, ck: usize, rflag: usize, cflag: usize| {
            let (rv, cv) = (p.v[rk] as i128, p.v[ck] as i128);
            let r = if p.v[rflag] != 0 { rv + row } else { rv };
            let c = if p.v[cflag] != 0 { cv + col } else { cv };
            format!(
                "{}{}{}{}",
                if p.v[cflag] != 0 { "" } else { "$" },
                col_letter(c),
                if p.v[rflag] != 0 { "" } else { "$" },
                r + 1
            )
        };
        let start = addr(0, 1, 4, 5);
        let end = addr(2, 3, 6, 7);
        if start == end {
            out.push_str(&start);
        } else {
            out.push_str(&start);
            out.push(':');
            out.push_str(&end);
        }
    }
    out
}

// ---------------------------------------------------------------- values

#[derive(Clone)]
enum Value {
    Str(String),
    Int(i64),
    Float(f64),
    IntFromFloat(f64), // an integral float too large for i64; Python makes it an int
}

#[derive(PartialEq)]
enum UKey {
    Int(i64),
    Str(String),
}

/// Python's round(x, 10): round the exact binary value to 10 decimals, half to even.
fn round10(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // Fast path: when the shortest round-trip repr has at most 10 decimals, it is a 10-decimal
    // grid point within half an ulp of x, so rounding to 10 decimals parses back to x itself.
    let mut buf = ryu::Buffer::new();
    let s = buf.format_finite(x);
    if !s.contains('e') {
        if let Some(dot) = s.find('.') {
            if s.len() - dot - 1 <= 10 {
                return x;
            }
        }
    }
    format!("{x:.10}").parse().unwrap_or(x)
}

fn clean_number(v: Value) -> Value {
    match v {
        Value::Float(n) if n.fract() == 0.0 && n.is_finite() => {
            if n.abs() < 9.2e18 {
                Value::Int(n as i64)
            } else {
                Value::IntFromFloat(n)
            }
        }
        Value::Float(n) => Value::Float(round10(n)),
        other => other,
    }
}

/// Days since 0001-01-01 style civil conversion (Howard Hinnant), from days since 1970-01-01.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Same as decoder.py `_excel_date`; None when the date is outside years 1-9999.
fn excel_date(serial: f64, kind: &str) -> Option<String> {
    const EPOCH_DAYS_FROM_1970: i64 = -25_569; // 1899-12-30
    // Like timedelta(days=serial): whole days exactly, the fraction rounded to microseconds.
    if !serial.is_finite() || serial.abs() > 1.0e7 {
        return None;
    }
    let whole = serial.trunc();
    let micros = whole as i64 * 86_400_000_000 + ((serial - whole) * 86_400_000_000.0).round_ties_even() as i64;
    let in_range = |days: i64| (1..=9999).contains(&civil_from_days(days + EPOCH_DAYS_FROM_1970).0);
    if !in_range(micros.div_euclid(86_400_000_000)) {
        return None; // the unrounded datetime is already out of range
    }
    let secs = (micros + 500_000).div_euclid(1_000_000);
    let days = secs.div_euclid(86_400) + EPOCH_DAYS_FROM_1970;
    let tod = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    let (hh, mm, ss) = (tod / 3600, tod / 60 % 60, tod % 60);
    Some(match kind {
        "date" => format!("{y:04}-{m:02}-{d:02}"),
        "time" => format!("{hh:02}:{mm:02}:{ss:02}"),
        _ => format!("{y:04}-{m:02}-{d:02} {hh:02}:{mm:02}:{ss:02}"),
    })
}

type Key = (u64, u64);

#[derive(Default)]
struct Out {
    sheet_id: String,
    cells: Vec<(Key, Value)>,
    formulas: Vec<(Key, String)>,
    styles: Vec<(Key, usize)>,
    images: Vec<(Key, (String, f64, f64))>,
    runs: Vec<(Key, Vec<(String, usize)>)>,
    merges: Vec<(u64, u64, u64, u64)>,
    col_widths: Vec<(i64, f64)>,
    row_heights: Vec<(i64, f64)>,
    unknown: Vec<(UKey, u64)>,
}

impl Out {
    fn unknown(&mut self, k: UKey) {
        match self.unknown.iter_mut().find(|(u, _)| *u == k) {
            Some((_, n)) => *n += 1,
            None => self.unknown.push((k, 1)),
        }
    }
}

struct Resolver<'a> {
    t: &'a Tables,
    sheet: &'a str,
    names: &'a HashMap<String, String>,
}

impl Resolver<'_> {
    fn number(&self, out: &mut Out, idx: i64) -> Option<Value> {
        if idx < INLINE_INT_LIMIT {
            return Some(Value::Int(idx));
        }
        let i = idx - NUMBER_TABLE_OFFSET;
        if i >= 0 && (i as usize) < self.t.numbers.len() {
            return Some(Value::Float(self.t.numbers[i as usize]));
        }
        out.unknown(UKey::Str(format!("bad_number_index:{idx}")));
        None
    }

    fn value(&self, out: &mut Out, vtype: i64, idx: i64, key: Key, depth: u32) -> Option<Value> {
        let t = self.t;
        let table = match vtype {
            TYPE_STRING => &t.plain,
            TYPE_RICH_STRING => {
                if depth == 0 && idx >= 0 {
                    if let Some(Some(runs)) = t.rich_runs.get(idx as usize) {
                        out.runs.push((key, runs.clone()));
                    }
                }
                &t.rich
            }
            TYPE_NUMBER => return self.number(out, idx),
            TYPE_FORMULA if depth == 0 => {
                let Some(&(ctype, cidx, tmpl)) = (idx >= 0).then(|| t.formulas.get(idx as usize)).flatten() else {
                    out.unknown(UKey::Str(format!("bad_formula_index:{idx}")));
                    return None;
                };
                if tmpl >= 0 {
                    if let Some(pieces) = t.templates.get(tmpl as usize) {
                        let f = render_formula(pieces, key.0 as i128, key.1 as i128, self.sheet, self.names);
                        out.formulas.push((key, f));
                    }
                }
                return match ctype {
                    None => Some(Value::Str(String::new())),
                    Some(ct) => self.value(out, ct, cidx, key, depth + 1),
                };
            }
            _ => {
                out.unknown(UKey::Int(vtype));
                return None;
            }
        };
        if idx >= 0 && (idx as usize) < table.len() {
            return Some(Value::Str(table[idx as usize].clone()));
        }
        out.unknown(UKey::Str(format!("bad_index:{vtype}")));
        None
    }
}

fn picture(buf: &[u8], span: Span) -> Res<Option<(String, f64, f64)>> {
    let Some(s) = first_sub(buf, span, 1)? else { return Ok(None) };
    let Ok(serde_json::Value::Array(a)) = serde_json::from_str::<serde_json::Value>(&text(buf, s)) else {
        return Ok(None);
    };
    if a.len() < 3 {
        return Ok(None);
    }
    let url = match &a[0] {
        serde_json::Value::String(u) => u.clone(),
        _ => return Ok(None),
    };
    let num = |v: &serde_json::Value| match v {
        serde_json::Value::Number(n) => n.as_f64().filter(|x| x.is_finite()),
        _ => None,
    };
    Ok(match (num(&a[1]), num(&a[2])) {
        (Some(w), Some(h)) => Some((url, w, h)),
        _ => None,
    })
}

fn read_cells(buf: &[u8], sheet_span: Span, out: &mut Out, r: &Resolver) -> Res<()> {
    let t = r.t;
    for cell in subs(buf, sheet_span, 6) {
        let cell = cell?;
        let (mut row, mut col) = (0u64, 0u64);
        let mut value_span = None;
        for f in fields(buf, cell) {
            match f? {
                (1, Val::Varint(v)) => row = v,
                (2, Val::Varint(v)) => col = v,
                (3, Val::Span(v)) => value_span = Some(v),
                _ => {}
            }
        }
        let Some(value_span) = value_span else { continue };
        let key = (row, col);

        let (mut vtype, mut index, mut style, mut kind, mut extra) = (None, 0i64, None, None, None);
        for f in fields(buf, value_span) {
            match f? {
                (1, Val::Varint(v)) => vtype = Some(v as i64),
                (2, Val::Span(v)) => index = int_or0(buf, v)?,
                (4, Val::Span(v)) => style = Some(int_or0(buf, v)?),
                (16, Val::Varint(v)) => kind = Some(v),
                (17, Val::Span(v)) => extra = Some(v),
                _ => {}
            }
        }
        let style = style.filter(|&s| s >= 0).map(|s| s as usize);
        if let Some(Some(id)) = style.and_then(|s| t.style_ids.get(s)) {
            out.styles.push((key, *id));
        }
        if kind == Some(CELL_KIND_PICTURE) {
            if let Some(extra) = extra {
                match picture(buf, extra)? {
                    Some(img) => out.images.push((key, img)),
                    None => out.unknown(UKey::Str("bad_picture".into())),
                }
            }
        }
        let Some(vtype) = vtype else { continue }; // style-only (empty) cell

        let Some(value) = r.value(out, vtype, index, key, 0) else { continue };
        let value = match value {
            Value::Int(_) | Value::Float(_) => {
                let date = style.and_then(|s| t.style_kinds.get(s).copied().flatten());
                let serial = match value {
                    Value::Int(n) => n as f64,
                    Value::Float(n) => n,
                    _ => unreachable!(),
                };
                match date.and_then(|k| excel_date(serial, k)) {
                    Some(s) => Value::Str(s),
                    None => clean_number(value),
                }
            }
            other => other,
        };
        out.cells.push((key, value));
    }
    Ok(())
}

fn read_merge(buf: &[u8], block: Span, out: &mut Out) -> Res<()> {
    for m in subs(buf, block, 6) {
        let m = m?;
        for rng in subs(buf, m, 3) {
            let rng = rng?;
            let mut v = [0u64; 6];
            for f in fields(buf, rng) {
                if let (g @ 2..=5, Val::Varint(x)) = f? {
                    v[g as usize] = x;
                }
            }
            out.merges.push((v[2], v[3], v[4], v[5]));
        }
    }
    Ok(())
}

fn read_sizes(buf: &[u8], block: Span, out: &mut Out) -> Res<()> {
    for sizes in subs(buf, block, 14) {
        let sizes = sizes?;
        for entry in subs(buf, sizes, 4) {
            let entry = entry?;
            let axis = int_in(buf, entry, 1)?.unwrap_or(0);
            let (mut first, mut last) = (0i64, 0i64);
            for r in subs(buf, entry, 2) {
                let r = r?;
                first = int_in(buf, r, 1)?.unwrap_or(0);
                last = int_in(buf, r, 2)?.unwrap_or(0);
            }
            let mut size = None;
            let is_row = match axis {
                1 => {
                    for props in subs(buf, entry, 3) {
                        let props = props?;
                        for h in subs(buf, props, 15) {
                            let h = h?;
                            size = double_in(buf, h)?;
                        }
                    }
                    true
                }
                2 => {
                    for props in subs(buf, entry, 4) {
                        let props = props?;
                        for f in fields(buf, props) {
                            match f? {
                                (9, Val::Span(v)) => size = double_in(buf, v)?, // px
                                (27, Val::Span(v)) if size.is_none() => {
                                    // Excel character units
                                    size = double_in(buf, v)?.map(|chars| chars * 7.0 + 5.0);
                                }
                                _ => {}
                            }
                        }
                    }
                    false
                }
                _ => continue,
            };
            if let Some(size) = size {
                if (last as i128) - (first as i128) < 20_000 {
                    let target = if is_row { &mut out.row_heights } else { &mut out.col_widths };
                    target.extend((first..=last).map(|i| (i, size)));
                }
            }
        }
    }
    Ok(())
}

/// Sheet id of a block: `fno { 3 { 1: sheet_id } }`.
fn block_sheet(buf: &[u8], block: Span, fno: u64) -> Res<Option<String>> {
    for inner in subs(buf, block, fno) {
        let inner = inner?;
        for meta in subs(buf, inner, 3) {
            let meta = meta?;
            for f in fields(buf, meta) {
                if let (1, Val::Span(v)) = f? {
                    return Ok(Some(text(buf, v)));
                }
            }
        }
    }
    Ok(None)
}

fn decode(raw: &[u8], sheet_id: Option<&str>, names: &HashMap<String, String>) -> Res<(Out, Vec<Style>)> {
    let mut out = Out { sheet_id: sheet_id.unwrap_or("").to_string(), ..Out::default() };
    let mut styles = Interner::default();
    let mut tables = Tables::default();
    let other_sheet = |owner: &Option<String>| matches!((sheet_id, owner), (Some(want), Some(o)) if o != want);

    for root in subs(raw, (0, raw.len()), 1) {
        let root = root?;
        for block in subs(raw, root, 5) {
            let block = block?;
            let (mut block_type, mut sheet_span) = (None, None);
            for f in fields(raw, block) {
                match f? {
                    (1, Val::Varint(v)) => block_type = Some(v as i64),
                    (19, Val::Span(v)) => sheet_span = Some(v),
                    _ => {}
                }
            }
            match block_type {
                Some(MERGE_BLOCK_TYPE) => {
                    if !other_sheet(&block_sheet(raw, block, 6)?) {
                        read_merge(raw, block, &mut out)?;
                    }
                    continue;
                }
                Some(SIZE_BLOCK_TYPE) => {
                    if !other_sheet(&block_sheet(raw, block, 14)?) {
                        read_sizes(raw, block, &mut out)?;
                    }
                    continue;
                }
                Some(CELL_BLOCK_TYPE) => {}
                _ => continue,
            }
            let Some(sheet_span) = sheet_span else { continue };
            let owner = block_sheet(raw, block, 19)?;
            if other_sheet(&owner) {
                continue; // a tab this one's formulas refer to
            }
            if sheet_id.is_none() {
                if let Some(o) = owner.as_ref().filter(|o| !o.is_empty()) {
                    out.sheet_id = o.clone();
                }
            }
            let value_tables = subs(raw, sheet_span, 5).collect::<Res<Vec<_>>>()?;
            if !value_tables.is_empty() {
                // each block carries its own tables; otherwise keep the previous ones
                tables = Tables {
                    style_kinds: std::mem::take(&mut tables.style_kinds),
                    style_ids: std::mem::take(&mut tables.style_ids),
                    ..Tables::default()
                };
                for vt in value_tables {
                    read_value_tables(raw, vt, &mut tables, &mut styles)?;
                }
            }
            for st in subs(raw, sheet_span, 4) {
                let st = st?;
                read_styles(raw, st, &mut tables, &mut styles)?;
            }
            let resolver = Resolver { t: &tables, sheet: owner.as_deref().unwrap_or(""), names };
            read_cells(raw, sheet_span, &mut out, &resolver)?;
        }
    }
    Ok((out, styles.list))
}

// ---------------------------------------------------------------- Python glue

fn value_py<'py>(py: Python<'py>, v: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match v {
        Value::Str(s) => s.into_pyobject(py)?.into_any(),
        Value::Int(n) => n.into_pyobject(py)?.into_any(),
        Value::Float(f) => f.into_pyobject(py)?.into_any(),
        Value::IntFromFloat(f) => py.get_type::<pyo3::types::PyInt>().call1((*f,))?,
    })
}

/// Decode a decompressed `related_sheet` payload.
///
/// Returns (sheet_id, cells, formulas, styles, cell_styles, images, runs, merges, col_widths,
/// row_heights, unknown), where `styles` is a list of Style field tuples and `cell_styles` / runs
/// refer to it by index.
#[pyfunction]
#[pyo3(signature = (raw, sheet_id=None, sheet_names=None))]
fn decode_raw<'py>(
    py: Python<'py>,
    raw: &[u8],
    sheet_id: Option<String>,
    sheet_names: Option<HashMap<String, String>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let names = sheet_names.unwrap_or_default();
    let (out, styles) = py
        .detach(|| {
            // A bug on some malformed payload must surface as a DecodeError, not a PanicException.
            std::panic::catch_unwind(|| decode(raw, sheet_id.as_deref(), &names))
                .unwrap_or_else(|_| Err("internal decoder error (panic) on this payload".into()))
        })
        .map_err(PyValueError::new_err)?;

    let cells = PyDict::new(py);
    for (k, v) in &out.cells {
        cells.set_item(k, value_py(py, v)?)?;
    }
    let formulas = PyDict::new(py);
    for (k, f) in &out.formulas {
        formulas.set_item(k, f)?;
    }
    let style_list = PyList::empty(py);
    for s in &styles {
        style_list.append((
            s.bold,
            s.color.as_deref(),
            s.size,
            s.font.as_deref(),
            s.fill.as_deref(),
            s.h_align,
            s.v_align,
            s.wrap,
            s.border,
        ))?;
    }
    let cell_styles = PyDict::new(py);
    for (k, i) in &out.styles {
        cell_styles.set_item(k, i)?;
    }
    let images = PyDict::new(py);
    for (k, img) in &out.images {
        images.set_item(k, img)?;
    }
    let runs = PyDict::new(py);
    for (k, r) in &out.runs {
        runs.set_item(k, r)?;
    }
    let merges = PyList::new(py, &out.merges)?;
    let col_widths = PyDict::new(py);
    for (k, v) in &out.col_widths {
        col_widths.set_item(k, v)?;
    }
    let row_heights = PyDict::new(py);
    for (k, v) in &out.row_heights {
        row_heights.set_item(k, v)?;
    }
    let unknown = PyList::empty(py);
    for (k, n) in &out.unknown {
        let key = match k {
            UKey::Int(i) => i.into_pyobject(py)?.into_any(),
            UKey::Str(s) => s.into_pyobject(py)?.into_any(),
        };
        unknown.append((key, n))?;
    }
    PyTuple::new(
        py,
        [
            out.sheet_id.into_pyobject(py)?.into_any(),
            cells.into_any(),
            formulas.into_any(),
            style_list.into_any(),
            cell_styles.into_any(),
            images.into_any(),
            runs.into_any(),
            merges.into_any(),
            col_widths.into_any(),
            row_heights.into_any(),
            unknown.into_any(),
        ],
    )
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode_raw, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_matches_python() {
        assert_eq!(round10(0.00048828125), 0.0004882812); // exact tie: half to even
        assert_eq!(round10(4876.3600000000015), 4876.36);
    }

    #[test]
    fn dates() {
        assert_eq!(excel_date(46218.0, "date").unwrap(), "2026-07-15");
        assert_eq!(excel_date(0.5, "time").unwrap(), "12:00:00");
        assert!(excel_date(1e12, "date").is_none());
        assert_eq!(excel_date(48379.92002893518, "datetime").unwrap(), "2032-06-14 22:04:50");
        assert!(excel_date(-693593.000001, "date").is_none());
        assert!(excel_date(2958465.99999537, "date").is_none());
    }

    #[test]
    fn sheet_names() {
        assert_eq!(sheet_prefix("Sheet1"), "Sheet1!");
        assert_eq!(sheet_prefix("AB12"), "'AB12'!");
        assert_eq!(sheet_prefix("关卡"), "'关卡'!");
        assert_eq!(sheet_prefix("it's"), "'it''s'!");
    }
}

#[cfg(test)]
mod bench {
    #[test]
    #[ignore]
    fn time_decode() {
        let path = std::env::var("RAW").unwrap();
        let raw = std::fs::read(path).unwrap();
        let names = std::collections::HashMap::new();
        for _ in 0..3 {
            let t = std::time::Instant::now();
            let (out, _) = super::decode(&raw, None, &names).unwrap();
            eprintln!("decode {:?} cells {}", t.elapsed(), out.cells.len());
        }
    }
}
