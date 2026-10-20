# coding: utf-8
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    # JSON keys are hyphenated like the rest of the API; Python names are accepted too.
    model_config = ConfigDict(populate_by_name=True, extra='forbid')


class Spectrum(_Model):
    """Flat-power comb to propagate. Omitted fields default to the equipment's SI."""

    f_min: Optional[float] = Field(None, alias='f-min', description='Lowest frequency [Hz]')
    f_max: Optional[float] = Field(None, alias='f-max', description='Highest frequency [Hz]')
    spacing: Optional[float] = Field(None, gt=0, description='Channel spacing [Hz]')
    baud_rate: Optional[float] = Field(None, alias='baud-rate', gt=0, description='Symbol rate [Baud]')
    roll_off: Optional[float] = Field(None, alias='roll-off', ge=0, le=1)
    tx_osnr: Optional[float] = Field(None, alias='tx-osnr', description='Transmitter OSNR [dB]')
    tx_power_dbm: Optional[float] = Field(None, alias='tx-power-dbm', description='Launch power per channel [dBm]')
    delta_pdb: float = Field(0.0, alias='delta-pdb', description='Per-channel power offset [dB]')


class PropagationRequest(_Model):
    path: List[str] = Field(..., min_length=2, description='Ordered element uids, typically transceiver to transceiver')
    spectrum: Optional[Spectrum] = None
    include_channels: bool = Field(False, alias='include-channels',
                                   description='Also return the metrics of every channel, not only the worst one')


class PathComputationRequest(_Model):
    source: str = Field(..., description='Source transceiver uid')
    destination: str = Field(..., description='Destination transceiver uid')
    k: int = Field(1, ge=1, le=20, description='Number of shortest paths to return')
