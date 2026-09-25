'''*!*! Request models for shared feature and annotation updates.'''

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class FeaturePatch(BaseModel):
    '''*!*! Optimistic update to feature geometry or properties.'''

    expected_version: int = Field(ge=1)
    geometry: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None

    @model_validator(mode='after')
    def require_change(self):
        '''*!*! Reject requests that contain no feature changes.'''

        if self.geometry is None and self.properties is None:
            raise ValueError('geometry or properties is required')
        if self.geometry is not None:
            geometry_type = self.geometry.get('type')
            if geometry_type not in {
                'Point',
                'MultiPoint',
                'Polygon',
                'MultiPolygon',
            }:
                raise ValueError('geometry must be a supported point or polygon type')
        return self


class AnnotationSubmission(BaseModel):
    '''*!*! Current annotation submitted by one assigned reviewer.'''

    label: str = Field(min_length=1, max_length=100)
    notes: str = Field(default='', max_length=5000)
    feature_version_seen: int = Field(ge=1)
