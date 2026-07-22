from django import forms
from .models import BillingUpload


class BillingUploadForm(forms.ModelForm):
    """Form to upload billing/usage CSV files with Bootstrap styling."""
    
    class Meta:
        model = BillingUpload
        fields = ("stored_file", "remarks")
        labels = {
            "stored_file": "Select CSV File",
            "remarks": "Notes / Remarks",
        }
        help_texts = {
            "stored_file": "Only CSV (.csv) reports are supported. Max file size: 100 MB.",
            "remarks": "Optional notes regarding the billing or usage cycle.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Inject form-control classes for styling
        self.fields["stored_file"].widget.attrs["class"] = "form-control"
        self.fields["remarks"].widget.attrs["class"] = "form-control"
        self.fields["remarks"].widget.attrs["rows"] = 4
        self.fields["remarks"].widget.attrs["placeholder"] = "Provide any specific context or remarks for this file..."
        
    def full_clean(self):
        super().full_clean()
        for field_name, field in self.fields.items():
            if field_name in self.errors:
                existing_classes = field.widget.attrs.get('class', '')
                if 'is-invalid' not in existing_classes:
                    field.widget.attrs['class'] = f'{existing_classes} is-invalid'.strip()
