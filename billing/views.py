from __future__ import annotations

import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from rest_framework import generics
from rest_framework.parsers import MultiPartParser

from .models import BillingRecord, BillingUpload
from .serializers import BillingRecordSerializer, BillingUploadSerializer
from .tasks import process_upload
from .forms import BillingUploadForm



class BillingUploadListCreateView(generics.ListCreateAPIView):
    queryset = BillingUpload.objects.all().order_by("-created_at")
    serializer_class = BillingUploadSerializer
    parser_classes = [MultiPartParser]

    def perform_create(self, serializer):
        upload = serializer.save(status="pending")
        process_upload(upload.id)


class BillingUploadDetailView(generics.RetrieveAPIView):
    queryset = BillingUpload.objects.all()
    serializer_class = BillingUploadSerializer


class BillingRecordListView(generics.ListAPIView):
    serializer_class = BillingRecordSerializer

    def get_queryset(self):
        return BillingRecord.objects.filter(upload_id=self.kwargs["upload_id"]).order_by("usage_date")


# ==========================================
# MODULE 2 - HTML View Handlers
# ==========================================

from django.core.exceptions import PermissionDenied
from accounts.permissions import get_active_project, has_project_permission


@login_required
def upload_billing_view(request):
    """View to handle OCI Billing CSV uploads."""
    project = get_active_project(request)
    if not project:
        return redirect("accounts:project-list")

    if not has_project_permission(request.user, project, "UPLOAD_BILLING"):
        raise PermissionDenied("You do not have permission to upload billing reports.")

    if request.method == "POST":
        form = BillingUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.save(commit=False)
            upload.uploaded_by = request.user
            upload.project = project
            upload.upload_type = "Billing Report"
            upload.upload_status = "Uploaded"
            upload.save()
            
            # Trigger synchronous processing
            process_upload(upload.id)
            
            messages.success(request, f"Billing report '{upload.original_filename}' uploaded and processed successfully.")
            return redirect("upload-history")
        else:
            messages.error(request, "Upload failed. Please fix the validation errors below.")
    else:
        form = BillingUploadForm()
        
    return render(request, "billing/upload.html", {
        "form": form,
        "upload_type": "Billing Report"
    })


@login_required
def upload_usage_view(request):
    """View to handle OCI Usage CSV uploads."""
    project = get_active_project(request)
    if not project:
        return redirect("accounts:project-list")

    if not has_project_permission(request.user, project, "UPLOAD_BILLING"):
        raise PermissionDenied("You do not have permission to upload usage reports.")

    if request.method == "POST":
        form = BillingUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.save(commit=False)
            upload.uploaded_by = request.user
            upload.project = project
            upload.upload_type = "Usage Report"
            upload.upload_status = "Uploaded"
            upload.save()
            
            # Trigger synchronous processing
            process_upload(upload.id)
            
            messages.success(request, f"Usage report '{upload.original_filename}' uploaded and processed successfully.")
            return redirect("upload-history")
        else:
            messages.error(request, "Upload failed. Please fix the validation errors below.")
    else:
        form = BillingUploadForm()
        
    return render(request, "billing/upload.html", {
        "form": form,
        "upload_type": "Usage Report"
    })


@login_required
def upload_history_view(request):
    """View to display all billing and usage report uploads."""
    project = get_active_project(request)
    if not project:
        return redirect("accounts:project-list")

    if not has_project_permission(request.user, project, "VIEW_BILLING"):
        raise PermissionDenied("You do not have permission to view billing history.")

    uploads = BillingUpload.objects.filter(project=project).order_by("-uploaded_at")
    return render(request, "billing/upload_history.html", {
        "uploads": uploads
    })


@login_required
def upload_detail_view(request, pk):
    """View to display details of a single report upload."""
    project = get_active_project(request)
    if not project:
        return redirect("accounts:project-list")

    if not has_project_permission(request.user, project, "VIEW_BILLING"):
        raise PermissionDenied("You do not have permission to view upload details.")

    upload = get_object_or_404(BillingUpload, pk=pk)
    from accounts.models import OrganizationMembership
    if not OrganizationMembership.objects.filter(user=request.user, organization=upload.project.organization).exists():
        raise PermissionDenied("You do not have permission to view this upload.")

    return render(request, "billing/upload_detail.html", {
        "upload": upload
    })


@login_required
def upload_delete_view(request, pk):
    """View to delete a single report upload along with its physical file."""
    upload = get_object_or_404(BillingUpload, pk=pk)
    
    # Ownership and staff restrictions
    if upload.uploaded_by != request.user and not request.user.is_staff:
        messages.error(request, "You do not have permission to delete this report.")
        return redirect("upload-history")

    if request.method == "POST":
        original_name = upload.original_filename
        # Delete file from filesystem
        if upload.stored_file:
            upload.stored_file.delete(save=False)
        if upload.uploaded_file and upload.uploaded_file != upload.stored_file:
            upload.uploaded_file.delete(save=False)
        # Delete record
        upload.delete()
        messages.success(request, f"Report '{original_name}' deleted successfully.")
        return redirect("upload-history")
        
    return render(request, "billing/delete_confirm.html", {
        "upload": upload
    })

