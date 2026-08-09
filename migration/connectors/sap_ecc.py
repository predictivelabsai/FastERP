"""Fail-closed placeholder for a future SAP ECC connector."""

from .sap_s4_odata import SapS4ODataConnector


class SapEccConnector(SapS4ODataConnector):
    """Reserve a stable connector identity while keeping ECC disabled."""

    connector_type = "sap_ecc"
    product_name = "SAP ECC"
