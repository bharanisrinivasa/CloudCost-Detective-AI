from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import User


class BootstrapFormMixin:
    """Mixin to apply Bootstrap styling to form fields dynamically."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            existing_classes = field.widget.attrs.get('class', '')
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = f'{existing_classes} form-check-input'.strip()
            elif isinstance(field.widget, (forms.FileInput, forms.ClearableFileInput)):
                field.widget.attrs['class'] = f'{existing_classes} form-control'.strip()
            else:
                field.widget.attrs['class'] = f'{existing_classes} form-control'.strip()

    def full_clean(self):
        super().full_clean()
        for field_name, field in self.fields.items():
            if field_name in self.errors:
                existing_classes = field.widget.attrs.get('class', '')
                if 'is-invalid' not in existing_classes:
                    field.widget.attrs['class'] = f'{existing_classes} is-invalid'.strip()



class CustomUserCreationForm(BootstrapFormMixin, UserCreationForm):
    """Custom user creation form with custom fields and validation."""
    
    email = forms.EmailField(
        required=True,
        help_text="Required. A valid email address is needed for notifications."
    )
    organization = forms.CharField(
        required=False,
        max_length=200,
        help_text="Optional. The name of your organization/company."
    )
    phone_number = forms.CharField(
        required=False,
        max_length=20,
        help_text="Optional. Contact number."
    )
    profile_picture = forms.ImageField(
        required=False,
        help_text="Optional. Upload a profile image."
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = UserCreationForm.Meta.fields + ("email", "organization", "phone_number", "profile_picture")

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email address already exists.")
        return email


class UserProfileForm(BootstrapFormMixin, forms.ModelForm):
    """Form to allow users to update their profile information."""
    
    email = forms.EmailField(
        required=True,
        help_text="Required. A valid email address is required."
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "organization", "phone_number", "profile_picture")

    def clean_email(self):
        email = self.cleaned_data.get("email")
        # Ensure email is unique across other users
        if email and User.objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email address already exists.")
        return email


class CustomAuthenticationForm(BootstrapFormMixin, AuthenticationForm):
    """Custom login authentication form with Bootstrap styling."""
    pass

