"""Migration source connector implementations."""

from .base import Capability, ExtractionPage, SourceConnector, SourceRecord
from .csv_bundle import CsvBundleConnector
from .erpnext_rest import ErpNextRestConnector
from .mock_sap import MockSapConnector
from .sap_business_one import SapBusinessOneConnector
from .sap_ecc import SapEccConnector
from .sap_s4_odata import SapS4ODataConnector

__all__ = [
    "Capability",
    "CsvBundleConnector",
    "ErpNextRestConnector",
    "ExtractionPage",
    "MockSapConnector",
    "SapBusinessOneConnector",
    "SapEccConnector",
    "SapS4ODataConnector",
    "SourceConnector",
    "SourceRecord",
]
