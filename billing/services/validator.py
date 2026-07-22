from django.core.exceptions import ValidationError

HEADER_MAPPING = {
    "service": ["service", "lineitem/service", "servicename", "service name", "product/servicename", "servicedescription"],
    "resource_name": ["resource_name", "resource name", "resourcename", "product/resourcename", "lineitem/resourcename", "product/description"],
    "resource_id": ["resource_id", "resource id", "resourceid", "product/resourceid", "lineitem/resourceid", "resourceid"],
    "compartment": ["compartment", "compartment_name", "compartmentname", "compartment name", "lineitem/compartmentname", "lineitem/compartmentpath"],
    "region": ["region", "regionname", "region name", "lineitem/region", "regionname"],
    "availability_domain": ["availability_domain", "availabilitydomain", "availability domain", "product/availabilitydomain", "availabilityzone"],
    "usage_start": ["usage_start", "usage start", "usagestart", "lineitem/intervalusagestart", "chargeperiodstart", "usage_date", "usagedate", "usage date"],
    "usage_end": ["usage_end", "usage end", "usageend", "lineitem/intervalusageend", "chargeperiodend"],
    "usage_quantity": ["usage_quantity", "usage quantity", "usagequantity", "usage/billedquantity", "pricingquantity", "billed_quantity", "quantity"],
    "usage_unit": ["usage_unit", "usage unit", "usageunit", "product/unit", "usage/billedquantityunit", "unit"],
    "cost": ["cost", "amount", "cost/mycost", "cost/cost", "mycost", "lineitem/cost"],
    "currency": ["currency", "cost/currency", "currencycode"],
    "tags": ["tags", "lineitem/tags", "tags/oracle-tags"]
}


class CSVHeaderValidator:
    @staticmethod
    def validate(headers: list[str]) -> dict[str, int]:
        """
        Validate that essential columns exist in the list of CSV headers.
        Returns a dict mapping the standard field names to their corresponding column index.
        """
        headers_cleaned = [h.strip().lower() for h in headers]
        col_map = {}

        for target, synonyms in HEADER_MAPPING.items():
            col_map[target] = None
            for syn in synonyms:
                if syn.lower() in headers_cleaned:
                    col_map[target] = headers_cleaned.index(syn.lower())
                    break

        if col_map["service"] is None:
            raise ValidationError("Missing required service column in CSV headers.")
        if col_map["cost"] is None:
            raise ValidationError("Missing required cost or amount column in CSV headers.")

        return col_map
