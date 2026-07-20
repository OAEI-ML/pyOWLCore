package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;

import java.util.List;
import org.junit.jupiter.api.Test;

final class CanonicalTest {
    @Test
    void iriMatchesModelSchemaOne() {
        assertArrayEquals(
                new byte[] {1, 2, 8, 'u', 'r', 'n', ':', 't', 'e', 's', 't'},
                Canonical.iri("urn:test"));
    }

    @Test
    void setsAreSortedAndDeduplicated() {
        assertArrayEquals(
                new byte[] {33, 6, 2, 1, 1, 1, 2},
                Canonical.node(
                        Canonical.OBJECT_ONE_OF,
                        Canonical.set(List.of(new byte[] {2}, new byte[] {1}, new byte[] {2}))));
    }
}
