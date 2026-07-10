import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET

from myapp.models import Item

from .exporters.csv_export import export_items_csv
from .exporters.json_export import export_items_json
from .models import ImportJob
from .tasks import process_import_job

ALLOWED_EXTENSIONS = {
    'catima_csv': ('.csv',),
    'native_csv': ('.csv',),
    'native_json': ('.json',),
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


@login_required
def upload_import(request):
    if request.method == 'POST':
        source_type = request.POST.get('source_type')
        upload = request.FILES.get('file')

        if source_type not in dict(ImportJob.SOURCE_CHOICES):
            messages.error(request, _('Please choose a valid import format.'))
        elif not upload:
            messages.error(request, _('Please choose a file to upload.'))
        elif upload.size > MAX_UPLOAD_SIZE:
            messages.error(request, _('File is too large (max 10MB).'))
        elif not upload.name.lower().endswith(ALLOWED_EXTENSIONS[source_type]):
            messages.error(request, _('File extension does not match the selected format.'))
        else:
            job = ImportJob.objects.create(user=request.user, source_type=source_type, file=upload)
            try:
                process_import_job.delay(str(job.id))
            except Exception as exc:
                job.status = 'failed'
                job.errors = [{'row': None, 'message': f'Could not queue the import task: {exc}'}]
                job.save(update_fields=['status', 'errors'])
                messages.error(request, _('Could not start the import — the background task queue is unreachable. Please contact your administrator.'))
                return redirect('import_job_status', job_id=job.id)

            messages.success(request, _('Import started! Refresh this page to see progress.'))
            return redirect('import_job_status', job_id=job.id)

    jobs = ImportJob.objects.filter(user=request.user)[:20]
    return render(request, 'imports/upload.html', {'jobs': jobs})


@login_required
def import_job_status(request, job_id):
    job = get_object_or_404(ImportJob, id=job_id, user=request.user)
    return render(request, 'imports/job_status.html', {'job': job})


@require_GET
@login_required
def export_csv(request):
    items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags')
    csv_text = export_items_csv(items)
    response = HttpResponse(csv_text, content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="vouchervault-export.csv"'
    return response


@require_GET
@login_required
def export_json(request):
    items = Item.objects.filter(user=request.user).select_related('wallet').prefetch_related('tags')
    payload = json.dumps(export_items_json(items), indent=2)
    response = HttpResponse(payload, content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="vouchervault-export.json"'
    return response
