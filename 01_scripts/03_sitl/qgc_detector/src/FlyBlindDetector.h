#pragma once

#include <QtCore/QObject>

#include <cstdint>

/// FlyBlind C2 state-freshness detector (second contribution).
///
/// Measures the Age-of-Information (AoI) of the vehicle state as seen at the GCS,
/// from the GLOBAL_POSITION_INT `time_boot_ms` stamp — which the autopilot writes
/// at generation, BEFORE the 5G uplink queue. This is an honest, receiver-side AoI
/// that uses no simulator ground truth (unlike belief-vs-truth):
///
///   impliedBootEpoch_i = arrival_wall_i - time_boot_ms_i   = T0 + D_i   (D_i >= 0)
///   estT0              = min_i impliedBootEpoch_i           ~= T0        (freshest sample)
///   AoI_i             = arrival_wall_i - (estT0 + time_boot_ms_i)  = D_i (transport age)
///
/// Calibrates estT0 as the minimum observed impliedBootEpoch, so a fresh (baseline)
/// stretch is needed to calibrate — which the FlyBlind protocol guarantees (the GCS
/// connects before the attack). Native QGC monitors watch link liveness (heartbeat,
/// which the attack keeps alive); this watches state freshness (which the attack ages).
class FlyBlindDetector : public QObject
{
    Q_OBJECT
    Q_PROPERTY(double telemetryAgeMs READ telemetryAgeMs NOTIFY telemetryChanged)
    Q_PROPERTY(bool telemetryStale READ telemetryStale NOTIFY telemetryChanged)
    Q_PROPERTY(double staleThresholdMs READ staleThresholdMs CONSTANT)

public:
    explicit FlyBlindDetector(QObject *parent = nullptr);

    /// Process-wide instance (created on first use, app lifetime).
    static FlyBlindDetector *instance();

    double telemetryAgeMs() const { return _ageMs; }
    bool telemetryStale() const { return _stale; }
    double staleThresholdMs() const { return _staleThresholdMs; }

    /// Feed the autopilot boot timestamp (ms) of one fresh GLOBAL_POSITION_INT.
    void handleGlobalPositionInt(uint32_t timeBootMs);

signals:
    void telemetryChanged();

private:
    qint64 _bootEpochEstMs = 0;
    bool _bootValid = false;
    double _ageMs = 0.0;
    bool _stale = false;
    // 1 s cleanly separates baseline (~18 ms) from attack (~13 s) with huge margin.
    double _staleThresholdMs = 1000.0;
};
