//! Stable, disjoint registration seams for successor native capabilities.

pub(crate) mod ingestion;
pub(crate) mod views;

use std::collections::HashSet;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

#[derive(Clone, Copy, Debug)]
pub(crate) struct BindingFeatures {
    pub(crate) ingestion: &'static [&'static str],
    pub(crate) views: &'static [&'static str],
}

impl BindingFeatures {
    pub(crate) fn combined(self) -> PyResult<Vec<&'static str>> {
        validate_partition("ingestion", self.ingestion)?;
        validate_partition("views", self.views)?;
        let mut seen = HashSet::new();
        let mut combined = Vec::with_capacity(self.ingestion.len() + self.views.len());
        for feature in self.ingestion.iter().chain(self.views) {
            if !seen.insert(*feature) {
                return Err(PyRuntimeError::new_err(
                    "native ingestion/view feature partitions overlap",
                ));
            }
            combined.push(*feature);
        }
        combined.sort_unstable();
        Ok(combined)
    }
}

pub(crate) fn register(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<BindingFeatures> {
    ingestion::register(py, module)?;
    views::register(py, module)?;
    let features = BindingFeatures {
        ingestion: ingestion::FEATURES,
        views: views::FEATURES,
    };
    features.combined()?;
    Ok(features)
}

fn validate_partition(name: &str, features: &[&str]) -> PyResult<()> {
    if features
        .iter()
        .any(|feature| feature.is_empty() || !feature.is_ascii())
        || features.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return Err(PyRuntimeError::new_err(format!(
            "native {name} feature partition must be ASCII ascending unique",
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_successor_partitions_fail_closed() {
        let features = BindingFeatures {
            ingestion: ingestion::FEATURES,
            views: views::FEATURES,
        };
        assert!(features.ingestion.is_empty());
        assert!(features.views.is_empty());
        assert!(features.combined().unwrap().is_empty());
    }

    #[test]
    fn partitions_must_be_disjoint() {
        let features = BindingFeatures {
            ingestion: &["shared-capability-v1"],
            views: &["shared-capability-v1"],
        };
        assert!(features.combined().is_err());
    }
}
