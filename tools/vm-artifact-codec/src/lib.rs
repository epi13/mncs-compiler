//! Lossless, standalone DAG encoding for frozen SSA metadata and code.
//! Nodes are interned in deterministic postorder; objects use sorted keys.
//! Identity strings, types, evidence and source correlation are preserved.
use serde::de::{
    self, DeserializeOwned, EnumAccess, IntoDeserializer, MapAccess, SeqAccess, VariantAccess,
    Visitor,
};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
enum Node {
    N,
    B(bool),
    I(serde_json::Number),
    S(String),
    A(Vec<u32>),
    O(Vec<(u32, u32)>),
}
#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Graph {
    nodes: Vec<Node>,
    root: u32,
}
struct Builder {
    nodes: Vec<Node>,
    ids: HashMap<u64, Vec<u32>>,
}
impl Builder {
    fn intern(&mut self, node: Node) -> u32 {
        use std::hash::{Hash, Hasher};
        let mut hash = std::collections::hash_map::DefaultHasher::new();
        node.hash(&mut hash);
        let hash = hash.finish();
        let bucket = self.ids.entry(hash).or_default();
        for id in bucket.iter() {
            if self.nodes[*id as usize] == node {
                return *id;
            }
        }
        let id = u32::try_from(self.nodes.len()).expect("artifact node count fits u32");
        bucket.push(id);
        self.nodes.push(node);
        id
    }
}
fn encode<T: Serialize>(value: &T) -> Result<Graph, Error> {
    // Walk typed SSA directly: no full compiler-tree clone or intermediate
    // JSON Value. Hash buckets are lookup-only; traversal defines node order.
    let mut builder = Builder {
        nodes: Vec::new(),
        ids: HashMap::new(),
    };
    let root = value.serialize(&mut builder)?;
    Ok(canonical_order(builder.nodes, root))
}
/// Freeze once for compiler-side sealing and transport. The caller can hash
/// and serialize this same immutable graph without rebuilding its tables.
pub fn freeze<T: Serialize>(value: &T) -> Result<serde_json::Value, Error> {
    serde_json::to_value(encode(value)?)
}
pub fn serialize<T: Serialize, S: Serializer>(value: &T, s: S) -> Result<S::Ok, S::Error> {
    encode(value)
        .map_err(serde::ser::Error::custom)?
        .serialize(s)
}
// Canonical postorder follows sorted object keys regardless of struct field
// emission order. Moving nodes avoids cloning the interned strings/metadata.
fn canonical_order(nodes: Vec<Node>, root: u32) -> Graph {
    fn visit(
        id: u32,
        old: &mut [Option<Node>],
        mapped: &mut [Option<u32>],
        out: &mut Vec<Node>,
    ) -> u32 {
        if let Some(id) = mapped[id as usize] {
            return id;
        }
        let mut node = old[id as usize].take().expect("acyclic interned node");
        match &mut node {
            Node::A(a) => {
                for r in a {
                    *r = visit(*r, old, mapped, out);
                }
            }
            Node::O(o) => {
                for (k, v) in o {
                    *k = visit(*k, old, mapped, out);
                    *v = visit(*v, old, mapped, out);
                }
            }
            _ => {}
        }
        let new = out.len() as u32;
        out.push(node);
        mapped[id as usize] = Some(new);
        new
    }
    let mut mapped = vec![None; nodes.len()];
    let mut out = Vec::with_capacity(nodes.len());
    let mut old: Vec<_> = nodes.into_iter().map(Some).collect();
    let root = visit(root, &mut old, &mut mapped, &mut out);
    Graph { nodes: out, root }
}
struct Compound<'a> {
    builder: &'a mut Builder,
    values: Vec<u32>,
    fields: Vec<(u32, u32)>,
    key: Option<u32>,
    variant: Option<u32>,
    object: bool,
}
impl<'a> Compound<'a> {
    fn new(builder: &'a mut Builder, object: bool, variant: Option<u32>) -> Self {
        Self {
            builder,
            values: Vec::new(),
            fields: Vec::new(),
            key: None,
            variant,
            object,
        }
    }
    fn field<T: Serialize + ?Sized>(&mut self, k: &str, v: &T) -> Result<(), Error> {
        let key = self.builder.intern(Node::S(k.into()));
        let value = v.serialize(&mut *self.builder)?;
        self.fields.push((key, value));
        Ok(())
    }
    fn finish(mut self) -> Result<u32, Error> {
        let node = if self.object {
            self.fields.sort_by(|(a, _), (b, _)| {
                let Node::S(a) = &self.builder.nodes[*a as usize] else {
                    unreachable!()
                };
                let Node::S(b) = &self.builder.nodes[*b as usize] else {
                    unreachable!()
                };
                a.cmp(b)
            });
            Node::O(self.fields)
        } else {
            Node::A(self.values)
        };
        let id = self.builder.intern(node);
        Ok(if let Some(key) = self.variant {
            self.builder.intern(Node::O(vec![(key, id)]))
        } else {
            id
        })
    }
}
impl<'a> Serializer for &'a mut Builder {
    type Ok = u32;
    type Error = Error;
    type SerializeSeq = Compound<'a>;
    type SerializeTuple = Compound<'a>;
    type SerializeTupleStruct = Compound<'a>;
    type SerializeTupleVariant = Compound<'a>;
    type SerializeMap = Compound<'a>;
    type SerializeStruct = Compound<'a>;
    type SerializeStructVariant = Compound<'a>;
    fn serialize_bool(self, v: bool) -> Result<u32, Error> {
        Ok(self.intern(Node::B(v)))
    }
    fn serialize_i8(self, v: i8) -> Result<u32, Error> {
        self.serialize_i64(v.into())
    }
    fn serialize_i16(self, v: i16) -> Result<u32, Error> {
        self.serialize_i64(v.into())
    }
    fn serialize_i32(self, v: i32) -> Result<u32, Error> {
        self.serialize_i64(v.into())
    }
    fn serialize_i64(self, v: i64) -> Result<u32, Error> {
        Ok(self.intern(Node::I(v.into())))
    }
    fn serialize_i128(self, v: i128) -> Result<u32, Error> {
        Ok(
            self.intern(Node::I(serde_json::Number::from_i128(v).ok_or_else(
                || serde::ser::Error::custom("integer outside frozen JSON range"),
            )?)),
        )
    }
    fn serialize_u8(self, v: u8) -> Result<u32, Error> {
        self.serialize_u64(v.into())
    }
    fn serialize_u16(self, v: u16) -> Result<u32, Error> {
        self.serialize_u64(v.into())
    }
    fn serialize_u32(self, v: u32) -> Result<u32, Error> {
        self.serialize_u64(v.into())
    }
    fn serialize_u64(self, v: u64) -> Result<u32, Error> {
        Ok(self.intern(Node::I(v.into())))
    }
    fn serialize_u128(self, v: u128) -> Result<u32, Error> {
        Ok(
            self.intern(Node::I(serde_json::Number::from_u128(v).ok_or_else(
                || serde::ser::Error::custom("integer outside frozen JSON range"),
            )?)),
        )
    }
    fn serialize_f32(self, v: f32) -> Result<u32, Error> {
        self.serialize_f64(v.into())
    }
    fn serialize_f64(self, v: f64) -> Result<u32, Error> {
        Ok(self.intern(
            serde_json::Number::from_f64(v)
                .map(Node::I)
                .unwrap_or(Node::N),
        ))
    }
    fn serialize_char(self, v: char) -> Result<u32, Error> {
        self.serialize_str(&v.to_string())
    }
    fn serialize_str(self, v: &str) -> Result<u32, Error> {
        Ok(self.intern(Node::S(v.into())))
    }
    fn serialize_bytes(self, v: &[u8]) -> Result<u32, Error> {
        v.serialize(self)
    }
    fn serialize_none(self) -> Result<u32, Error> {
        self.serialize_unit()
    }
    fn serialize_some<T: Serialize + ?Sized>(self, v: &T) -> Result<u32, Error> {
        v.serialize(self)
    }
    fn serialize_unit(self) -> Result<u32, Error> {
        Ok(self.intern(Node::N))
    }
    fn serialize_unit_struct(self, _: &'static str) -> Result<u32, Error> {
        self.serialize_unit()
    }
    fn serialize_unit_variant(
        self,
        _: &'static str,
        _: u32,
        v: &'static str,
    ) -> Result<u32, Error> {
        self.serialize_str(v)
    }
    fn serialize_newtype_struct<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        v: &T,
    ) -> Result<u32, Error> {
        v.serialize(self)
    }
    fn serialize_newtype_variant<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        _: u32,
        k: &'static str,
        v: &T,
    ) -> Result<u32, Error> {
        let key = self.intern(Node::S(k.into()));
        let value = v.serialize(&mut *self)?;
        Ok(self.intern(Node::O(vec![(key, value)])))
    }
    fn serialize_seq(self, _: Option<usize>) -> Result<Compound<'a>, Error> {
        Ok(Compound::new(self, false, None))
    }
    fn serialize_tuple(self, _: usize) -> Result<Compound<'a>, Error> {
        self.serialize_seq(None)
    }
    fn serialize_tuple_struct(self, _: &'static str, _: usize) -> Result<Compound<'a>, Error> {
        self.serialize_seq(None)
    }
    fn serialize_tuple_variant(
        self,
        _: &'static str,
        _: u32,
        k: &'static str,
        _: usize,
    ) -> Result<Compound<'a>, Error> {
        let key = self.intern(Node::S(k.into()));
        Ok(Compound::new(self, false, Some(key)))
    }
    fn serialize_map(self, _: Option<usize>) -> Result<Compound<'a>, Error> {
        Ok(Compound::new(self, true, None))
    }
    fn serialize_struct(self, _: &'static str, _: usize) -> Result<Compound<'a>, Error> {
        self.serialize_map(None)
    }
    fn serialize_struct_variant(
        self,
        _: &'static str,
        _: u32,
        k: &'static str,
        _: usize,
    ) -> Result<Compound<'a>, Error> {
        let key = self.intern(Node::S(k.into()));
        Ok(Compound::new(self, true, Some(key)))
    }
}
impl serde::ser::SerializeSeq for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_element<T: Serialize + ?Sized>(&mut self, v: &T) -> Result<(), Error> {
        self.values.push(v.serialize(&mut *self.builder)?);
        Ok(())
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeTuple for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_element<T: Serialize + ?Sized>(&mut self, v: &T) -> Result<(), Error> {
        serde::ser::SerializeSeq::serialize_element(self, v)
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeTupleStruct for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_field<T: Serialize + ?Sized>(&mut self, v: &T) -> Result<(), Error> {
        serde::ser::SerializeSeq::serialize_element(self, v)
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeTupleVariant for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_field<T: Serialize + ?Sized>(&mut self, v: &T) -> Result<(), Error> {
        serde::ser::SerializeSeq::serialize_element(self, v)
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeMap for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_key<T: Serialize + ?Sized>(&mut self, k: &T) -> Result<(), Error> {
        let id = k.serialize(&mut *self.builder)?;
        if !matches!(self.builder.nodes[id as usize], Node::S(_)) {
            return Err(serde::ser::Error::custom("object key must be a string"));
        }
        self.key = Some(id);
        Ok(())
    }
    fn serialize_value<T: Serialize + ?Sized>(&mut self, v: &T) -> Result<(), Error> {
        let key = self
            .key
            .take()
            .ok_or_else(|| serde::ser::Error::custom("missing map key"))?;
        let value = v.serialize(&mut *self.builder)?;
        self.fields.push((key, value));
        Ok(())
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeStruct for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        k: &'static str,
        v: &T,
    ) -> Result<(), Error> {
        self.field(k, v)
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
impl serde::ser::SerializeStructVariant for Compound<'_> {
    type Ok = u32;
    type Error = Error;
    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        k: &'static str,
        v: &T,
    ) -> Result<(), Error> {
        self.field(k, v)
    }
    fn end(self) -> Result<u32, Error> {
        self.finish()
    }
}
pub fn deserialize<'de, T: DeserializeOwned, D: Deserializer<'de>>(d: D) -> Result<T, D::Error> {
    let graph = Graph::deserialize(d)?;
    graph.validate().map_err(de::Error::custom)?;
    T::deserialize(At {
        graph: &graph,
        id: graph.root,
    })
    .map_err(de::Error::custom)
}
impl Graph {
    fn validate(&self) -> Result<(), String> {
        if self.nodes.is_empty() || self.root as usize != self.nodes.len() - 1 {
            return Err("DAG root must be the final node".into());
        }
        // Bound expansion before typed decoding. Shared nodes cannot create
        // exponential expansion, host stack overflow, or unbounded allocation.
        // These are decoder limits, independent of VM execution budgets.
        let mut sizes: Vec<u64> = Vec::with_capacity(self.nodes.len());
        let mut depths: Vec<u32> = Vec::with_capacity(self.nodes.len());
        let mut reached = vec![false; self.nodes.len()];
        reached[self.root as usize] = true;
        for (i, n) in self.nodes.iter().enumerate() {
            let refs: Vec<u32> = match n {
                Node::A(a) => a.clone(),
                Node::O(o) => o.iter().flat_map(|(k, v)| [*k, *v]).collect(),
                _ => Vec::new(),
            };
            let mut size = 1u64;
            let mut depth = 1;
            for r in refs {
                if r as usize >= i {
                    return Err("DAG references must point backward".into());
                }
                size = size.saturating_add(sizes[r as usize]);
                depth = depth.max(depths[r as usize] + 1);
            }
            if depth > 128 || size > 8_000_000 {
                return Err("DAG expansion exceeds decoder limits".into());
            }
            if let Node::O(o) = n {
                let mut prev: Option<&str> = None;
                for (k, _) in o {
                    let Node::S(key) = &self.nodes[*k as usize] else {
                        return Err("object key must be a string node".into());
                    };
                    if prev.is_some_and(|p| p >= key.as_str()) {
                        return Err("object keys must be unique and sorted".into());
                    }
                    prev = Some(key);
                }
            }
            sizes.push(size);
            depths.push(depth);
        }
        for i in (0..self.nodes.len()).rev() {
            if !reached[i] {
                return Err("unreachable DAG node".into());
            }
            match &self.nodes[i] {
                Node::A(a) => {
                    for r in a {
                        reached[*r as usize] = true
                    }
                }
                Node::O(o) => {
                    for (k, v) in o {
                        reached[*k as usize] = true;
                        reached[*v as usize] = true
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }
}
#[derive(Clone, Copy)]
struct At<'a> {
    graph: &'a Graph,
    id: u32,
}
impl<'a> At<'a> {
    fn child(self, id: u32) -> Self {
        Self {
            graph: self.graph,
            id,
        }
    }
}
type Error = serde_json::Error;
impl<'de> Deserializer<'de> for At<'_> {
    type Error = Error;
    fn deserialize_any<V: Visitor<'de>>(self, v: V) -> Result<V::Value, Error> {
        match &self.graph.nodes[self.id as usize] {
            Node::N => v.visit_unit(),
            Node::B(b) => v.visit_bool(*b),
            Node::I(n) => serde_json::Value::Number(n.clone())
                .into_deserializer()
                .deserialize_any(v),
            Node::S(s) => v.visit_str(s),
            Node::A(a) => v.visit_seq(Seq {
                at: self,
                iter: a.iter(),
            }),
            Node::O(o) => v.visit_map(Map {
                at: self,
                iter: o.iter(),
                value: None,
            }),
        }
    }
    fn deserialize_i128<V: Visitor<'de>>(self, v: V) -> Result<V::Value, Error> {
        match &self.graph.nodes[self.id as usize] {
            Node::I(n) => serde_json::Value::Number(n.clone())
                .into_deserializer()
                .deserialize_i128(v),
            _ => self.deserialize_any(v),
        }
    }
    fn deserialize_u128<V: Visitor<'de>>(self, v: V) -> Result<V::Value, Error> {
        match &self.graph.nodes[self.id as usize] {
            Node::I(n) => serde_json::Value::Number(n.clone())
                .into_deserializer()
                .deserialize_u128(v),
            _ => self.deserialize_any(v),
        }
    }
    fn deserialize_option<V: Visitor<'de>>(self, v: V) -> Result<V::Value, Error> {
        if matches!(self.graph.nodes[self.id as usize], Node::N) {
            v.visit_none()
        } else {
            v.visit_some(self)
        }
    }
    fn deserialize_newtype_struct<V: Visitor<'de>>(
        self,
        _: &'static str,
        v: V,
    ) -> Result<V::Value, Error> {
        v.visit_newtype_struct(self)
    }
    fn deserialize_enum<V: Visitor<'de>>(
        self,
        _: &'static str,
        _: &'static [&'static str],
        v: V,
    ) -> Result<V::Value, Error> {
        match &self.graph.nodes[self.id as usize] {
            Node::S(s) => v.visit_enum(s.as_str().into_deserializer()),
            Node::O(o) if o.len() == 1 => {
                let (key, value) = o[0];
                v.visit_enum(Enum {
                    key: self.child(key),
                    value: self.child(value),
                })
            }
            _ => Err(de::Error::custom("expected externally tagged enum")),
        }
    }
    serde::forward_to_deserialize_any! {bool i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 char str string bytes byte_buf unit unit_struct seq tuple tuple_struct map struct identifier ignored_any}
}
struct Seq<'a> {
    at: At<'a>,
    iter: std::slice::Iter<'a, u32>,
}
impl<'de> SeqAccess<'de> for Seq<'_> {
    type Error = Error;
    fn next_element_seed<T: de::DeserializeSeed<'de>>(
        &mut self,
        s: T,
    ) -> Result<Option<T::Value>, Error> {
        self.iter
            .next()
            .map(|r| s.deserialize(self.at.child(*r)))
            .transpose()
    }
    fn size_hint(&self) -> Option<usize> {
        Some(self.iter.len())
    }
}
struct Map<'a> {
    at: At<'a>,
    iter: std::slice::Iter<'a, (u32, u32)>,
    value: Option<u32>,
}
impl<'de> MapAccess<'de> for Map<'_> {
    type Error = Error;
    fn next_key_seed<K: de::DeserializeSeed<'de>>(
        &mut self,
        s: K,
    ) -> Result<Option<K::Value>, Error> {
        self.iter
            .next()
            .map(|(k, v)| {
                self.value = Some(*v);
                s.deserialize(self.at.child(*k))
            })
            .transpose()
    }
    fn next_value_seed<V: de::DeserializeSeed<'de>>(&mut self, s: V) -> Result<V::Value, Error> {
        s.deserialize(
            self.at.child(
                self.value
                    .take()
                    .ok_or_else(|| de::Error::custom("missing map value"))?,
            ),
        )
    }
    fn size_hint(&self) -> Option<usize> {
        Some(self.iter.len())
    }
}
struct Enum<'a> {
    key: At<'a>,
    value: At<'a>,
}
impl<'de, 'a> EnumAccess<'de> for Enum<'a> {
    type Error = Error;
    type Variant = At<'a>;
    fn variant_seed<V: de::DeserializeSeed<'de>>(self, s: V) -> Result<(V::Value, At<'a>), Error> {
        Ok((s.deserialize(self.key)?, self.value))
    }
}
impl<'de> VariantAccess<'de> for At<'_> {
    type Error = Error;
    fn unit_variant(self) -> Result<(), Error> {
        Deserialize::deserialize(self)
    }
    fn newtype_variant_seed<T: de::DeserializeSeed<'de>>(self, s: T) -> Result<T::Value, Error> {
        s.deserialize(self)
    }
    fn tuple_variant<V: Visitor<'de>>(self, _: usize, v: V) -> Result<V::Value, Error> {
        self.deserialize_any(v)
    }
    fn struct_variant<V: Visitor<'de>>(
        self,
        _: &'static [&'static str],
        v: V,
    ) -> Result<V::Value, Error> {
        self.deserialize_any(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Wrapped {
        #[serde(with = "crate")]
        value: serde_json::Value,
    }
    #[test]
    fn exact_deterministic_roundtrip() {
        let w = Wrapped {
            value: serde_json::json!({"a":[{"same":"identity","n":18446744073709551615u64},null,true],"b":{"same":"identity","n":18446744073709551615u64}}),
        };
        let b = serde_json::to_vec(&w).unwrap();
        assert_eq!(serde_json::from_slice::<Wrapped>(&b).unwrap(), w);
        assert_eq!(b, serde_json::to_vec(&w).unwrap());
    }
    #[test]
    fn malformed_graphs_fail_closed() {
        for data in [
            r#"{"nodes":[{"A":[0]}],"root":0}"#,
            r#"{"nodes":["N","N"],"root":1}"#,
            r#"{"nodes":[{"I":1},{"O":[[0,0]]}],"root":1}"#,
        ] {
            let g: Graph = serde_json::from_str(data).unwrap();
            assert!(g.validate().is_err());
        }
    }
    #[test]
    fn exponential_expansion_is_refused() {
        let mut nodes = vec![Node::N];
        for i in 0..30 {
            nodes.push(Node::A(vec![i, i]));
        }
        assert!(Graph { nodes, root: 30 }.validate().is_err());
    }
}

#[cfg(test)]
mod canonical_tests {
    use super::*;
    #[derive(Serialize, Deserialize, PartialEq, Debug)]
    struct Row {
        z: i128,
        a: String,
    }
    #[derive(Serialize, Deserialize)]
    struct Artifact {
        #[serde(with = "crate")]
        module: Row,
    }
    #[test]
    fn canonical_order_is_independent_of_struct_order_and_freezes_once() {
        let row = Row {
            z: i64::MIN.into(),
            a: "α identity".into(),
        };
        assert_eq!(
            freeze(&row).unwrap(),
            freeze(&serde_json::to_value(&row).unwrap()).unwrap()
        );
        let artifact = Artifact { module: row };
        let bytes = serde_json::to_vec(&artifact).unwrap();
        let decoded: Artifact = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(decoded.module, artifact.module);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&bytes).unwrap()["module"],
            freeze(&artifact.module).unwrap()
        );
    }
}
