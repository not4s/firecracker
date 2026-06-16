// Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! Auxiliary module for configuring the metrics system.
use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::logger::{FcLineWriter, METRICS};
use crate::utils::open_file_nonblock;

/// Maximum number of metrics properties that can be configured.
const MAX_PROPERTIES: usize = 10;
/// Maximum length (in bytes) of a metrics property key.
const MAX_KEY_LEN: usize = 64;
/// Maximum length (in bytes) of a metrics property value.
const MAX_VALUE_LEN: usize = 512;

/// Strongly typed structure used to describe the metrics system.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct MetricsConfig {
    /// Named pipe or file used as output for metrics.
    pub metrics_path: PathBuf,
    /// Optional operator-defined key-value properties emitted on every metrics line.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub properties: Option<BTreeMap<String, String>>,
}

/// Errors associated with actions on the `MetricsConfig`.
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum MetricsConfigError {
    /// Cannot initialize the metrics system due to bad user input: {0}
    InitializationFailure(String),
    /// Too many metrics properties: {0} (maximum is {1}).
    TooManyProperties(usize, usize),
    /// A metrics property key exceeds the maximum length of {0} bytes.
    KeyTooLong(usize),
    /// A metrics property value exceeds the maximum length of {0} bytes.
    ValueTooLong(usize),
}

/// Validates operator-supplied metrics properties against the configured size limits.
fn validate_properties(properties: &BTreeMap<String, String>) -> Result<(), MetricsConfigError> {
    if properties.len() > MAX_PROPERTIES {
        return Err(MetricsConfigError::TooManyProperties(
            properties.len(),
            MAX_PROPERTIES,
        ));
    }
    for (key, value) in properties {
        if key.len() > MAX_KEY_LEN {
            return Err(MetricsConfigError::KeyTooLong(MAX_KEY_LEN));
        }
        if value.len() > MAX_VALUE_LEN {
            return Err(MetricsConfigError::ValueTooLong(MAX_VALUE_LEN));
        }
    }
    Ok(())
}

/// Configures the metrics as described in `metrics_cfg`.
pub fn init_metrics(metrics_cfg: MetricsConfig) -> Result<(), MetricsConfigError> {
    if let Some(properties) = &metrics_cfg.properties {
        validate_properties(properties)?;
    }

    let writer = FcLineWriter::new(
        open_file_nonblock(&metrics_cfg.metrics_path)
            .map_err(|err| MetricsConfigError::InitializationFailure(err.to_string()))?,
    );
    METRICS
        .init(writer)
        .map_err(|err| MetricsConfigError::InitializationFailure(err.to_string()))?;

    if let Some(properties) = metrics_cfg.properties {
        METRICS
            .properties
            .set(properties)
            .map_err(|err| MetricsConfigError::InitializationFailure(err.to_string()))?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use vmm_sys_util::tempfile::TempFile;

    use super::*;

    #[test]
    fn test_init_metrics() {
        // Initializing metrics with valid pipe is ok.
        let metrics_file = TempFile::new().unwrap();
        let desc = MetricsConfig {
            metrics_path: metrics_file.as_path().to_path_buf(),
            properties: None,
        };

        init_metrics(desc.clone()).unwrap();
        init_metrics(desc).unwrap_err();
    }

    #[test]
    fn test_validate_properties() {
        let mut props = BTreeMap::new();
        props.insert("customer_id".to_string(), "1234".to_string());
        props.insert("bundle_id".to_string(), "fn-abc".to_string());
        validate_properties(&props).unwrap();

        let too_many: BTreeMap<String, String> = (0..=MAX_PROPERTIES)
            .map(|i| (i.to_string(), "v".to_string()))
            .collect();
        validate_properties(&too_many).unwrap_err();

        let mut long_key = BTreeMap::new();
        long_key.insert("k".repeat(MAX_KEY_LEN + 1), "v".to_string());
        validate_properties(&long_key).unwrap_err();

        let mut long_value = BTreeMap::new();
        long_value.insert("k".to_string(), "v".repeat(MAX_VALUE_LEN + 1));
        validate_properties(&long_value).unwrap_err();
    }
}
