#include "FlyBlindDetector.h"

#include <QtCore/QApplicationStatic>
#include <QtCore/QDateTime>

Q_APPLICATION_STATIC(FlyBlindDetector, _flyBlindDetectorInstance);

FlyBlindDetector::FlyBlindDetector(QObject *parent)
    : QObject(parent)
{
}

FlyBlindDetector *FlyBlindDetector::instance()
{
    return _flyBlindDetectorInstance();
}

void FlyBlindDetector::handleGlobalPositionInt(uint32_t timeBootMs)
{
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    const qint64 implied = now - static_cast<qint64>(timeBootMs);   // = T0 + D
    if (!_bootValid || (implied < _bootEpochEstMs)) {               // min = freshest sample (D ~= 0)
        _bootEpochEstMs = implied;
        _bootValid = true;
    }

    double age = static_cast<double>(now - (_bootEpochEstMs + static_cast<qint64>(timeBootMs)));
    if (age < 0.0) {
        age = 0.0;
    }

    _ageMs = age;
    _stale = (age > _staleThresholdMs);
    emit telemetryChanged();
}
