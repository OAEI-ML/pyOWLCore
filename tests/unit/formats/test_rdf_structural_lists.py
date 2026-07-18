from __future__ import annotations

import pyowl_core.model as m
from pyowl_core import BackendPreference, LoadOptions, parse_document


def test_explicit_rdf_list_types_are_consumed_as_structural_markers() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="https://example.org/list"/>
  <rdfs:Datatype rdf:about="https://example.org/list#status">
    <owl:equivalentClass>
      <rdfs:Datatype>
        <owl:oneOf>
          <rdf:Description>
            <rdf:type rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#List"/>
            <rdf:first>active</rdf:first>
            <rdf:rest>
              <rdf:Description>
                <rdf:type rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#List"/>
                <rdf:first>retired</rdf:first>
                <rdf:rest rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
              </rdf:Description>
            </rdf:rest>
          </rdf:Description>
        </owl:oneOf>
      </rdfs:Datatype>
    </owl:equivalentClass>
  </rdfs:Datatype>
</rdf:RDF>
"""

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(backend=BackendPreference.PYTHON),
    )

    definition = next(document.iter_axioms(m.DatatypeDefinition))
    assert isinstance(definition, m.DatatypeDefinition)
    assert definition.datatype == m.Datatype(m.IRI("https://example.org/list#status"))
    assert definition.data_range == m.DataOneOf(
        m.CanonicalSet((m.Literal("active", m.XSD_STRING), m.Literal("retired", m.XSD_STRING)))
    )
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.consumed_triples == document.rdf_mapping_report.total_triples
