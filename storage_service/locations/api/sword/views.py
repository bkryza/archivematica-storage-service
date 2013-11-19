# stdlib, alphabetical
import datetime
from lxml import etree as etree
from multiprocessing import Process
import os
import shutil
import tempfile
import time

# Core Django, alphabetical
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

# This project, alphabetical
from locations.models import Deposit
from locations.models import Location
import helpers

"""
Example GET of service document:

  curl -v http://127.0.0.1/api/v2/sword
"""
def service_document(request):
    transfer_collectiion_url = request.build_absolute_uri(
        reverse('components.api.views_sword.transfer_collection')
    )

    service_document_xml = render_to_string('api/sword/service_document.xml', locals())
    response = HttpResponse(service_document_xml)
    response['Content-Type'] = 'application/atomserv+xml'
    return response

"""
Example GET of collection deposit list:

  curl -v http://localhost:8000/api/v1/location/96606387-cc70-4b09-b422-a7220606488d/sword/collection/

Example POST creation of deposit:

  curl -v -H "In-Progress: true" --data-binary @mets.xml --request POST http://localhost:8000/api/v1/location/96606387-cc70-4b09-b422-a7220606488d/sword/collection/
"""
# TODO: error if deposit is finalized, but has no files?
def collection(request, location_uuid):
    error = None
    bad_request = None

    if request.method == 'GET':
        # return list of deposits as ATOM feed
        col_iri = request.build_absolute_uri(
            reverse('sword_collection', kwargs={'api_name': 'v1',
                'resource_name': 'location', 'uuid': location_uuid}))

        feed = {
            'title': 'Deposits',
            'url': col_iri
        }

        entries = []

        for uuid in helpers.deposit_list(location_uuid):
            deposit = Deposit.objects.get(uuid=uuid)

            edit_iri = request.build_absolute_uri(
                reverse('sword_deposit', kwargs={'api_name': 'v1',
                    'resource_name': 'deposit', 'uuid': uuid}))

            entries.append({
                'title': deposit.name,
                'url': edit_iri,
            })

        collection_xml = render_to_string('locations/api/sword/collection.xml', locals())
        response = HttpResponse(collection_xml)
        response['Content-Type'] = 'application/atom+xml;type=feed'
        return response
    elif request.method == 'POST':
        # is the deposit still in progress?
        if 'HTTP_IN_PROGRESS' in request.META and request.META['HTTP_IN_PROGRESS'] == 'true':
            # process creation request, if criteria met
            if request.body != '':
                try:
                    temp_filepath = helpers.write_request_body_to_temp_file(request)

                    # parse name and content URLs out of XML
                    try:
                        tree = etree.parse(temp_filepath)
                        root = tree.getroot()
                        deposit_name = root.get('LABEL')

                        if deposit_name == None:
                            bad_request = 'No deposit name found in XML.'
                        else:
                            # assemble deposit specification
                            deposit_specification = {'location_uuid': location_uuid}
                            deposit_specification['name'] = deposit_name
                            if 'HTTP_ON_BEHALF_OF' in request.META:
                                # TODO: should get this from author header
                                deposit_specification['sourceofacquisition'] = request.META['HTTP_ON_BEHALF_OF']

                            location_path = helpers.deposit_location_path(location_uuid)
                            if not os.path.isdir(location_path):
                                error = _error(500, 'Location path (%s) does not exist: contact an administrator.' % (location_path))
                            else:
                                deposit_uuid = _create_deposit_directory_and_db_entry(deposit_specification)

                                if deposit_uuid != None:
                                    # parse XML for content URLs
                                    object_content_urls = []

                                    elements = root.iterfind("{http://www.loc.gov/METS/}fileSec/"
                                        + "{http://www.loc.gov/METS/}fileGrp[@ID='DATASTREAMS']/"
                                        + "{http://www.loc.gov/METS/}fileGrp[@ID='OBJ']/"
                                        + "{http://www.loc.gov/METS/}file/"
                                        + "{http://www.loc.gov/METS/}FLocat"
                                    )

                                    for element in elements:
                                        object_content_urls.append(element.get('{http://www.w3.org/1999/xlink}href'))

                                    # create process so content URLs can be downloaded asynchronously
                                    p = Process(target=_fetch_content, args=(deposit_uuid, object_content_urls))
                                    p.start()

                                    return _deposit_receipt_response(request, deposit_uuid, 201)
                                else:
                                    error = _error(500, 'Could not create deposit: contact an administrator.')
                    except etree.XMLSyntaxError as e:
                        error = _error(412, 'Error parsing XML ({error_message}).'.format(error_message=str(e)))
                except Exception as e:
                    bad_request = str(e)
            else:
                error = _error(412, 'A request body must be sent when creating a deposit.')
        else:
            # TODO: way to do one-step deposit creation by setting In-Progress to false
            error = _error(412, 'The In-Progress header must be set to true when creating a deposit.')
    else:
        error = _error(405, 'This endpoint only responds to the GET and POST HTTP methods.')

    if bad_request != None:
        error = _error(400, bad_request)

    if error != None:
        return _sword_error_response(request, error)

def _create_deposit_directory_and_db_entry(deposit_specification):
    if 'name' in deposit_specification:
        deposit_name = deposit_specification['name']
    else:
        deposit_name = 'Untitled'

    deposit_path = os.path.join(
        helpers.deposit_location_path(deposit_specification['location_uuid']),
        deposit_name
    )

    # TODO deposit_path = helpers.pad_destination_filepath_if_it_already_exists(deposit_path)
    os.mkdir(deposit_path)
    os.chmod(deposit_path, 02770) # drwxrws---

    if os.path.exists(deposit_path):
        location = Location.objects.get(uuid=deposit_specification['location_uuid'])
        deposit = Deposit.objects.create(name=deposit_name, path=deposit_name,
            location=location)

        # TODO
        if 'sourceofacquisition' in deposit_specification:
            deposit.source = deposit_specification['sourceofacquisition']

        deposit.save()
        return deposit.uuid

def _fetch_content(deposit_uuid, object_content_urls):
    # update deposit with number of files that need to be downloaded
    deposit = Deposit.objects.get(uuid=deposit_uuid)
    deposit.downloads_attempted = len(object_content_urls)
    deposit.downloads_completed = 0
    deposit.save()

    # download the files
    destination_path = helpers.deposit_storage_path(deposit_uuid)
    temp_dir = tempfile.mkdtemp()

    completed = 0
    for url in object_content_urls:
        try:
            filename = helpers.download_resource(url, temp_dir)
            completed += 1
        except:
            pass
        shutil.move(os.path.join(temp_dir, filename),
            os.path.join(destination_path, filename))

    # remove temp dir
    shutil.rmtree(temp_dir)

    # record the number of successful downloads and completion time
    deposit.downloads_completed = completed
    deposit.download_completion_time = timezone.now()
    deposit.save()

"""
TODO: decouple deposits and locations for shorter URLs

Example POST finalization of deposit:

  curl -v -H "In-Progress: false" --request POST http://127.0.0.1/api/v1/location/96606387-cc70-4b09-b422-a7220606488d/sword/deposit/5bdf83cd-5858-4152-90e2-c2426e90e7c0/

Example DELETE of deposit:

  curl -v -XDELETE http://127.0.0.1/api/v1/location/96606387-cc70-4b09-b422-a7220606488d/sword/deposit/5bdf83cd-5858-4152-90e2-c2426e90e7c0/
"""
# TODO: add authentication
def deposit(request, uuid):
    error = None
    bad_request = None

    if request.method == 'GET':
        # details about a deposit
        return HttpResponse('Feed XML of files for deposit' + uuid)
    elif request.method == 'POST':
        # is the deposit ready to be processed?
        if 'HTTP_IN_PROGRESS' in request.META and request.META['HTTP_IN_PROGRESS'] == 'false':
            # TODO: check that related tasks are complete before copying
            # ...task row must exist and task endtime must be equal to or greater than start time
            try:
                if _deposit_has_been_submitted_for_processing(uuid):
                    error = _error(400, 'This deposit has already been submitted for processing.')
                else:
                    deposit = Deposit.objects.get(uuid=uuid)

                    if len(os.listdir(deposit.full_path())) > 0:
                        """
                        TODO: replace this will call to dashboard API
                        helpers.copy_to_start_transfer(transfer.currentlocation, 'standard', {'uuid': uuid})

                        # wait for watch directory to determine a transfer is awaiting
                        # approval then attempt to approve it
                        time.sleep(5)
                        approve_transfer_via_mcp(
                            os.path.basename(transfer.currentlocation),
                            'standard',
                        1
                        ) # TODO: replace hardcoded user ID
                        """
                        pass

                        return _deposit_receipt_response(request, uuid, 200)
                    else:
                        bad_request = 'This deposit contains no files.'

            except ObjectDoesNotExist:
                error = _error(404, 'This deposit could not be found.')
        else:
            bad_request = 'The In-Progress header must be set to false when starting deposit processing.'
    elif request.method == 'PUT':
        # update deposit
        return HttpResponse(status=204) # No content
    elif request.method == 'DELETE':
        # delete deposit files
        deposit_path = helpers.deposit_storage_path(uuid)
        shutil.rmtree(deposit_path)

        # delete entry in Transfers table (and task?)
        deposit = Deposit.objects.get(uuid=uuid)
        deposit.delete()
        return HttpResponse(status=204) # No content
    else:
        error = _error(405, 'This endpoint only responds to the GET, POST, PUT, and DELETE HTTP methods.')

    if bad_request != None:
        error = _error(400, bad_request)

    if error != None:
        return _sword_error_response(request, error)

"""
Example GET of files list:

  curl -v http://127.0.0.1/api/v2/transfer/sword/03ce11a5-32c1-445a-83ac-400008894f78/media

Example POST of file:

  curl -v -H "Content-Disposition: attachment; filename=joke.jpg" --request POST \
    --data-binary "@joke.jpg" \
    http://localhost/api/v2/transfer/sword/03ce11a5-32c1-445a-83ac-400008894f78/media

Example DELETE of all files:

  curl -v -XDELETE \
      "http://localhost/api/v2/transfer/sword/03ce11a5-32c1-445a-83ac-400008894f78/media

Example DELETE of file:

  curl -v -XDELETE \
    "http://localhost/api/v2/transfer/sword/03ce11a5-32c1-445a-83ac-400008894f78/media?filename=thing.jpg"
"""
def deposit_media(request, uuid):
    if _deposit_has_been_submitted_for_processing(uuid):
        return _sword_error_response(request, {
            'summary': 'This deposit has already been submitted for processing.',
            'status': 400
        })

    error = None

    if request.method == 'GET':
        deposit_path = helpers.deposit_storage_path(uuid)
        if deposit_path == None:
            error = _error(404, 'This deposit does not exist.')
        else:
            if os.path.exists(deposit_path):
                return HttpResponse(str(os.listdir(deposit_path)))
            else:
                error = _error(404, 'This deposit path (%s) does not exist.' % (deposit_path))
    elif request.method == 'PUT':
        # replace a file in the deposit
        return _handle_upload_request(request, uuid, True)
    elif request.method == 'POST':
        # add a file to the deposit
        return _handle_upload_request(request, uuid)
    elif request.method == 'DELETE':
        filename = request.GET.get('filename', '')
        if filename != '':
            deposit_path = helpers.deposit_storage_path(uuid)
            file_path = os.path.join(deposit_path, filename) 
            if os.path.exists(file_path):
                os.remove(file_path)
                return HttpResponse(status=204) # No content
            else:
                error = _error(404, 'The path to this file (%s) does not exist.' % (file_path))
        else:
            # delete all files in deposit
            if _deposit_has_been_submitted_for_processing(uuid):
                error = _error(400, 'This deposit has already been submitted for processing.')
            else:
                deposit = Deposit.objects.get(uuid=uuid)

                for filename in os.listdir(deposit.full_path()):
                    filepath = os.path.join(deposit.full_path(), filename)
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                    elif os.path.isdir(filepath):
                        shutil.rmtree(filepath)

                return HttpResponse(status=204) # No content
    else:
        error = _error(405, 'This endpoint only responds to the GET, POST, PUT, and DELETE HTTP methods.')

    if error != None:
        return _sword_error_response(request, error)

def _handle_upload_request(request, uuid, replace_file=False):
    error = None
    bad_request = None

    if 'HTTP_CONTENT_DISPOSITION' in request.META:
        filename = helpers.parse_filename_from_content_disposition(request.META['HTTP_CONTENT_DISPOSITION']) 

        if filename != '':
            file_path = os.path.join(helpers.deposit_storage_path(uuid), filename)

            if replace_file:
                # if doing a file replace, the file being replaced must exist
                if os.path.exists(file_path):
                    return _handle_upload_request_with_potential_md5_checksum(
                        request,
                        file_path,
                        204
                    )
                else:
                    bad_request = 'File does not exist.'
            else:
                # if adding a file, the file must not already exist
                if os.path.exists(file_path):
                    bad_request = 'File already exists.'
                else:
                    return _handle_upload_request_with_potential_md5_checksum(
                        request,
                        file_path,
                        201
                    )
        else:
            bad_request = 'No filename found in Content-disposition header.'
    else:
        bad_request = 'Content-disposition must be set in request header.'

    if bad_request != None:
        error = _error(400, bad_request)

    if error != None:
        return _sword_error_response(request, error)

def _handle_upload_request_with_potential_md5_checksum(request, file_path, success_status_code):
    temp_filepath = helpers.write_request_body_to_temp_file(request)
    if 'HTTP_CONTENT_MD5' in request.META:
        md5sum = helpers.get_file_md5_checksum(temp_filepath)
        if request.META['HTTP_CONTENT_MD5'] != md5sum:
            os.remove(temp_filepath)
            bad_request = 'MD5 checksum of uploaded file ({uploaded_md5sum}) does not match checksum provided in header ({header_md5sum}).'.format(uploaded_md5sum=md5sum, header_md5sum=request.META['HTTP_CONTENT_MD5'])
            return _sword_error_response(request, {
                'summary': bad_request,
                'status': 400
            })
        else:
            shutil.copyfile(temp_filepath, file_path)
            os.remove(temp_filepath)
            return HttpResponse(status=success_status_code)
    else:
        shutil.copyfile(temp_filepath, file_path)
        os.remove(temp_filepath)
        return HttpResponse(status=success_status_code)

"""
Example GET of state:

  curl -v http://localhost:8000/api/v1/deposit/96606387-cc70-4b09-b422-a7220606488d/sword/state/
"""
# TODO: add authentication
def deposit_state(request, uuid):
    # TODO: add check if UUID is valid, 404 otherwise

    error = None

    if request.method == 'GET':
        """
        In order to determine the deposit status we need to check
        for three possibilities:
        
        1) The deposit involved no asynchronous depositing. The
           downloads_attempted DB row column should be 0.
       
        2) The deposit involved asynchronous depositing, but
           the depositing is incomplete. downloads_attempted is
           greater than 0, but download_completion_time is not
           set.
      
        3) The deposit involved asynchronous depositing and
           completed successfully. download_completion_time is set.
           downloads_attempted is equal to downloads_completed.
      
        4) The deposit involved asynchronous depositing and
           completed unsuccessfully. download_completion_time is set.
           downloads_attempted isn't equal to downloads_completed.
        """
        deposit = Deposit.objects.get(uuid=uuid)
        if deposit.downloads_attempted == 0:
            task_state = 'Complete'
        else:
           if deposit.download_completion_time == None:
               task_state = 'Incomplete'
           else:
               if deposit.downloads_attempted == deposit.downloads_completed:
                   task_state = 'Complete'
               else:
                   task_state = 'Failed'

        state_term = task_state.lower()
        state_description = 'Deposit initiation: ' + task_state

        response = HttpResponse(render_to_string('locations/api/sword/state.xml', locals()))
        response['Content-Type'] = 'application/atom+xml;type=feed'
        return response
    else:
        error = _error(405, 'This endpoint only responds to the GET HTTP method.')

    if error != None:
                return _sword_error_response(request, error)

# respond with SWORD 2.0 deposit receipt XML
def _deposit_receipt_response(request, deposit_uuid, status_code):
    deposit = Deposit.objects.get(uuid=deposit_uuid)

    # TODO: fix minor issues with template
    media_iri = request.build_absolute_uri(
        reverse('sword_deposit_media', kwargs={'api_name': 'v1',
            'resource_name': 'deposit', 'uuid': deposit_uuid}))

    edit_iri = request.build_absolute_uri(
        reverse('sword_deposit', kwargs={'api_name': 'v1',
            'resource_name': 'deposit', 'uuid': deposit_uuid}))

    state_iri = request.build_absolute_uri(
        reverse('sword_deposit_state', kwargs={'api_name': 'v1',
            'resource_name': 'deposit', 'uuid': deposit_uuid}))

    receipt_xml = render_to_string('locations/api/sword/deposit_receipt.xml', locals())

    response = HttpResponse(receipt_xml, mimetype='text/xml', status=status_code)
    response['Location'] = deposit_uuid
    return response

def _sword_error_response(request, error_details):
    error_details['request'] = request
    error_details['update_time'] = datetime.datetime.now().__str__()
    error_details['user_agent'] = request.META['HTTP_USER_AGENT']
    error_xml = render_to_string('locations/api/sword/error.xml', error_details)
    return HttpResponse(error_xml, status=error_details['status'])

def _error(status, summary):
    return {
        'summary': summary,
        'status': status
    }

# TODO: is this right?
def _deposit_has_been_submitted_for_processing(deposit_uuid):
    try:
        deposit = models.Deposit.objects.get(uuid=deposit_uuid)
        if deposit.status != 'complete':
            return True
        return False
    except:
        return False