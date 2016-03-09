# encoding: UTF-8
import itertools
import datetime

import autocomplete_light
from django.core.mail import send_mail
from django.http import HttpResponseRedirect
from django.contrib.auth.decorators import login_required, permission_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth.views import login as django_login
from django.shortcuts import get_object_or_404, render
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from voting.models import Vote
from generic_confirmation.views import confirm_by_get

from manager.forms import UserRegistrationForm, CollaboratorRegistrationForm, \
    InstallationForm, HardwareForm, RegistrationForm, InstallerRegistrationForm, \
    TalkProposalForm, TalkProposalImageCroppingForm, ContactMessageForm, \
    AttendeeSearchForm, AttendeeRegistrationByCollaboratorForm, InstallerRegistrationFromCollaboratorForm, \
    TalkForm, CommentForm, PresentationForm, EventoLUserRegistrationForm, AttendeeRegistrationForm
from manager.models import *
from manager.schedule import Schedule
from manager.security import is_installer, add_collaborator_perms


autocomplete_light.autodiscover()


def update_event_info(event_url, render_dict=None, event=None):
    event = event or Event.objects.get(url__iexact=event_url)
    contacts = Contact.objects.filter(event=event)
    render_dict = render_dict or {}
    render_dict.update({
        'event_url': event_url,
        'event': event,
        'contacts': contacts
    })
    return render_dict


def event_django_view(request, event_url, view=django_login):
    return view(request, extra_context=update_event_info(event_url))


def index(request, event_url):
    event = Event.objects.get(url__iexact=event_url)

    if event.external_url:
        msgs = messages.get_messages(request)
        if msgs:
            return render(request, 'base.html', update_event_info(event_url, {messages: msgs}, event))

        return HttpResponseRedirect(event.external_url)

    talk_proposals = TalkProposal.objects.filter(activity__event=event)\
        .exclude(image__isnull=True) \
        .exclude(image__url__exact='') \
        .distinct()

    render_dict = {'talk_proposals': talk_proposals}
    return render(request, 'index.html', update_event_info(event_url, render_dict, event))


def event_view(request, event_url, html='index.html'):
    return render(request, html, update_event_info(event_url))


def event(request, event_url):
    event = Event.objects.get(url__iexact=event_url)

    if event.external_url:
        return HttpResponseRedirect(event.external_url)

    render_dict = update_event_info(event_url, render_dict={'event_information': event.event_information}, event=event)
    return render(request, 'event/info.html', render_dict)


def home(request):
    events = Event.objects.all()
    return render(request, 'homepage.html', {'events': events})


def get_forms_errors(forms):
    field_errors = [form.non_field_errors() for form in forms]
    errors = [error for error in field_errors]
    return list(itertools.chain.from_iterable(errors))


def talk_registration(request, event_url, pk):
    errors = []
    error = False
    talk = None
    event = Event.objects.get(url__iexact=event_url)

    # FIXME: Esto es lo que se llama una buena chanchada!
    post = None
    if request.POST:
        start_time = datetime.datetime.strptime(request.POST.get('start_date', None), '%H:%M')
        end_time = datetime.datetime.strptime(request.POST.get('end_date', None), '%H:%M')

        start_time_posta = datetime.datetime.combine(event.date, start_time.time())
        end_time_posta = datetime.datetime.combine(event.date, end_time.time())

        post = request.POST.copy()

        post['start_date'] = start_time_posta.strftime('%Y-%m-%d %H:%M:%S')
        post['end_date'] = end_time_posta.strftime('%Y-%m-%d %H:%M:%S')

    # Fin de la chanchada

    talk_form = TalkForm(event_url, post)
    proposal = TalkProposal.objects.get(pk=pk)
    forms = [talk_form]
    if request.POST:
        if talk_form.is_valid() and room_available(request, talk_form.instance, event_url):
            try:
                proposal.confirmed = True
                proposal.save()
                talk = talk_form.save()
                talk.talk_proposal = proposal
                talk.save()
                messages.success(request, _("The talk was registered successfully!"))
                return HttpResponseRedirect(reverse("talk_detail", args=[event_url, talk.pk]))
            except Exception:
                if talk is not None:
                    Talk.delete(talk)
                if proposal.confirmed:
                    proposal.confirmed = False
                    proposal.save()
        errors = get_forms_errors(forms)
        error = True
        if errors:
            messages.error(request, _("The talk wasn't registered successfully (check form errors)"))
    comments = Comment.objects.filter(proposal=proposal)
    vote = Vote.objects.get_for_user(proposal, request.user)
    score = Vote.objects.get_score(proposal)
    render_dict = dict(comments=comments, comment_form=CommentForm(), user=request.user, proposal=proposal)
    if vote or score:
        render_dict.update({'vote': vote, 'score': score})

    render_dict.update({'multipart': False, 'errors': errors, 'form': talk_form, 'error': error})
    return render(request,
                  'talks/detail.html',
                  update_event_info(event_url, render_dict))


def room_available(request, talk_form, event_url):
    talks_room = Talk.objects.filter(room=talk_form.room, talk_proposal__event__url__iexact=event_url)
    if talk_form.start_date == talk_form.end_date:
        messages.error(request, _(
            "The talk wasn't registered successfully because schedule isn't available (start time equals end time)"))
        return False
    if talk_form.end_date < talk_form.start_date:
        messages.error(request, _(
            "The talk wasn't registered successfully because schedule isn't available (start time is after end time)"))
        return False

    one_second = datetime.timedelta(seconds=1)
    if talks_room.filter(
            end_date__range=(talk_form.start_date + one_second, talk_form.end_date - one_second)).exists() \
            or talks_room.filter(end_date__gt=talk_form.end_date, start_date__lt=talk_form.start_date).exists() \
            or talks_room.filter(
                    start_date__range=(talk_form.start_date + one_second, talk_form.end_date - one_second)).exists() \
            or talks_room.filter(end_date=talk_form.end_date, start_date=talk_form.start_date).exists():
        messages.error(request,
                       _("The talk wasn't registered successfully because the room or schedule isn't available"))
        return False
    return True


@login_required(login_url='../accounts/login/')
def become_installer(request, event_url):
    forms = []
    errors = []
    installer = None

    if request.POST:
        collaborator = Collaborator.objects.get(user=request.user)
        installer_form = InstallerRegistrationFromCollaboratorForm(request.POST,
                                                                   instance=Installer(collaborator=collaborator))
        forms = [installer_form]
        if installer_form.is_valid():
            try:
                installer = installer_form.save()
                collaborator.user = add_collaborator_perms(collaborator.user)
                collaborator.save()
                installer.save()
                messages.success(request, _("You've became an installer!"))
                return HttpResponseRedirect('/event/' + event_url)
            except Exception as e:
                if installer is not None:
                    Installer.delete(installer)
        messages.error(request, _("You not became an installer (check form errors)"))
        errors = get_forms_errors(forms)

    else:
        installer_form = InstallerRegistrationFromCollaboratorForm(instance=Installer())
        forms = [installer_form]

    return render(request,
                  'registration/become_installer.html',
                  update_event_info(event_url, {'forms': forms, 'errors': errors, 'multipart': False}))


@login_required(login_url='./accounts/login/')
@user_passes_test(is_installer)
def installation(request, event_url):
    installation_form = InstallationForm(event_url, request.POST or None, prefix='installation')
    hardware_form = HardwareForm(request.POST or None, prefix='hardware')
    forms = [installation_form, hardware_form]
    errors = []
    if request.POST:
        if hardware_form.is_valid():
            hardware = hardware_form.save()
            try:
                if installation_form.is_valid():
                    install = installation_form.save()
                    install.hardware = hardware
                    install.installer = Installer.objects.get(collaborator__user=request.user)
                    install.save()
                    messages.success(request, _("The installation has been registered successfully. Happy Hacking!"))
                    return HttpResponseRedirect('/event/' + event_url)
                else:
                    if hardware is not None:
                        Hardware.delete(hardware)
            except Exception:
                if hardware is not None:
                    Hardware.delete(hardware)
                if install is not None:
                    Installation.delete(installation)
        messages.error(request, _("The installation hasn't been registered successfully (check form errors)"))
        errors = get_forms_errors(forms)

    return render(request,
                  'installation/installation-form.html',
                  update_event_info(event_url, {'forms': forms, 'errors': errors, 'multipart': False}))


def confirm_registration(request, event_url, token):
    messages.success(request, _(
        'Thanks for your confirmation! You don\'t need to bring any paper to the event. You\'ll be asked for the '
        'email you registered with'))
    return confirm_by_get(request, token, success_url='/event/' + event_url)


@login_required(login_url='../../accounts/login/')
def talk_proposal(request, event_url, pk=None):
    event = Event.objects.get(url__iexact=event_url)

    if not event.talk_proposal_is_open:
        messages.error(request,
                       _(
                           "The talk proposal is already close or the event is not accepting proposals through this "
                           "page. Please contact the Event Organization Team to submit it."))
        return HttpResponseRedirect(reverse('index', args=(event_url,)))

    if pk:
        proposal = TalkProposal.objects.get(pk=pk)
    else:
        activity = Activity(event=event)
        proposal = TalkProposal(activity=activity)
    form = TalkProposalForm(request.POST or None, request.FILES or None, instance=proposal)
    if request.POST:
        if form.is_valid():
            proposal = form.save()
            return HttpResponseRedirect(reverse('image_cropping', args=(event_url, proposal.pk)))
        messages.error(request, _("The proposal hasn't been registered successfully (check form errors)"))

    return render(request, 'talks/proposal.html', update_event_info(event_url, {'form': form}))


@login_required(login_url='../../../accounts/login/')
def image_cropping(request, event_url, image_id):
    proposal = get_object_or_404(TalkProposal, pk=image_id)
    form = TalkProposalImageCroppingForm(request.POST or None, request.FILES, instance=proposal)
    if request.POST:
        if form.is_valid():
            # If a new file is being upload
            if request.FILES:
                # If clear home_image is clicked, delete the image
                if request.POST.get('home_image-clear') or request.FILES:
                    form.cleaned_data['home_image'] = None

                # Save the changes and redirect to upload a new one or crop the new one
                form.save()
                messages.info(request, _("Please crop or upload a new image."))
                return HttpResponseRedirect(reverse('image_cropping', args=(event_url, proposal.pk)))
            form.save()
            messages.success(request, _("The proposal has been registered successfully!"))
            return HttpResponseRedirect(reverse('proposal_detail', args=(event_url, proposal.pk)))
        messages.error(request, _("The proposal hasn't been registered successfully (check form errors)"))
    return render(request, 'talks/proposal/image-cropping.html', update_event_info(event_url, {'form': form}))


def schedule(request, event_url):
    event = Event.objects.get(url__iexact=event_url)
    if not event.schedule_confirm:
        messages.info(request, _("While the schedule this unconfirmed, you can only see the list of proposals."))
        return HttpResponseRedirect(reverse("talks", args=[event_url]))

    rooms = Room.objects.filter(event=event)
    talks_confirmed = Talk.objects.filter(talk_proposal__confirmed=True, talk_proposal__event=event)
    schedule = Schedule(list(rooms), list(talks_confirmed))
    return render(request, 'talks/schedule.html',
                  update_event_info(event_url, event=event, render_dict={'schedule': schedule}))


def talks(request, event_url):
    event = Event.objects.get(url__iexact=event_url)
    talks_list = Talk.objects.filter(talk_proposal__activity__event=event)
    proposals = TalkProposal.objects.filter(activity__event=event)
    for proposal in proposals:
        setattr(proposal, 'form', TalkForm(event_url))
        setattr(proposal, 'errors', [])
    return render(request, 'talks/talks_home.html',
                  update_event_info(event_url, {'talks': talks_list, 'proposals': proposals, 'event': event}, event))


def talk_detail(request, event_url, pk):
    talk = Talk.objects.get(pk=pk)
    return HttpResponseRedirect(reverse('proposal_detail', args=(event_url, talk.talk_proposal.pk)))


def talk_delete(request, event_url, pk):
    talk = Talk.objects.get(pk=pk)
    talk.talk_proposal.confirmed = False
    talk.talk_proposal.save()
    talk.delete()
    return HttpResponseRedirect(reverse('proposal_detail', args=(event_url, talk.talk_proposal.pk)))


def proposal_detail(request, event_url, pk):
    proposal = TalkProposal.objects.get(pk=pk)
    comments = Comment.objects.filter(proposal=proposal)
    render_dict = dict(comments=comments, comment_form=CommentForm(), user=request.user, proposal=proposal)
    vote = Vote.objects.get_for_user(proposal, request.user)
    score = Vote.objects.get_score(proposal)
    if vote or score:
        render_dict.update({'vote': vote, 'score': score})
    if proposal.confirmed:
        talk = Talk.objects.get(talk_proposal=proposal)
        render_dict.update({'talk': talk, 'form': TalkForm(event_url, instance=talk),
                            'form_presentation': PresentationForm(instance=proposal), 'errors': []})
    else:
        render_dict.update({'form': TalkForm(event_url), 'errors': []})
    return render(request, 'talks/detail.html', update_event_info(event_url, render_dict))


def upload_presentation(request, event_url, pk):
    proposal = get_object_or_404(TalkProposal, pk=pk)
    form = PresentationForm(request.POST or None, request.FILES, instance=proposal)
    if request.POST:
        if form.is_valid():
            if request.FILES:
                if request.POST.get('presentation-clear') or request.FILES:
                    form.cleaned_data['presentation'] = None
            form.save()
            messages.success(request, _("The presentation has been uploaded successfully!"))
            return HttpResponseRedirect(reverse('proposal_detail', args=(event_url, proposal.pk)))
        messages.error(request, _("The presentation hasn't been uploaded successfully (check form errors)"))
    return HttpResponseRedirect(reverse('proposal_detail', args=(event_url, pk)))


@login_required(login_url='../../accounts/login/')
@permission_required('manager.add_attendee', raise_exception=True)
def attendee_search(request, event_url):
    form = AttendeeSearchForm(event_url, request.POST or None)
    if request.POST:
        if form.is_valid():
            attendee_email = form.cleaned_data['attendee']
            if attendee_email is not None:
                attendee = Attendee.objects.get(email=attendee_email, event__url__iexact=event_url)
                if attendee.assisted:
                    messages.info(request, _('The attendee has already been registered correctly.'))
                else:
                    attendee.assisted = True
                    attendee.save()
                    messages.success(request, _('The attendee has been registered successfully. Happy Hacking!'))
                return HttpResponseRedirect(reverse("attendee_search", args=[event_url]))
            else:
                return HttpResponseRedirect('/event/' + event_url + '/registration/attendee/by-collaborator')
        messages.error(request, _("The attendee hasn't been registered successfully (check form errors)"))

    return render(request, 'registration/attendee/search.html', update_event_info(event_url, {'form': form}))


@login_required(login_url='../../accounts/login/')
@permission_required('manager.add_attendee', raise_exception=True)
def attendee_registration_by_collaborator(request, event_url):
    event = Event.objects.get(url__iexact=event_url)
    attendee = Attendee(eventolUser__event=event)
    form = AttendeeRegistrationByCollaboratorForm(request.POST or None, instance=attendee)
    if request.POST:
        if form.is_valid():
            attendee = form.save()
            attendee.assisted = True
            attendee.save()
            messages.success(request, _('The attendee has been registered successfully. Happy Hacking!'))
            return HttpResponseRedirect(reverse("attendee_search", args=(event_url, )))
        messages.error(request, _("The attendee hasn't been registered successfully (check form errors)"))

    return render(request, 'registration/attendee/by-collaborator.html', update_event_info(event_url, {'form': form}))


def contact(request, event_url):
    event = Event.objects.get(url__iexact=event_url)
    contact_message = ContactMessage()
    form = ContactMessageForm(request.POST or None, instance=contact_message)
    if request.POST:
        if form.is_valid():
            contact_message = form.save()
            send_mail(_("FLISoL Contact Message " + contact_message.name + " email " + contact_message.email),
                      contact_message.message,
                      contact_message.email,
                      recipient_list=[event.email, ],
                      fail_silently=False)
            contact_message.save()
            messages.success(request, _("The message has been sent."))
            return HttpResponseRedirect('/event/' + event_url)
        messages.error(request, _("The message hasn't been sent."))

    return render(request, 'contact-message.html', update_event_info(event_url, {'form': form}, event))


@login_required(login_url='../../../../../accounts/login/')
def delete_comment(request, event_url, pk, comment_pk=None):
    """Delete comment(s) with primary key `pk` or with pks in POST."""
    if request.user.is_staff:
        pklist = request.POST.getlist("delete") if not comment_pk else [comment_pk]
        for comment_pk in pklist:
            Comment.objects.get(pk=comment_pk).delete()
    return HttpResponseRedirect(reverse("proposal_detail", args=[event_url, pk]))


@login_required(login_url='../../../accounts/login/')
def add_comment(request, event_url, pk):
    """Add a new comment."""
    comment = Comment(proposal=TalkProposal.objects.get(pk=pk), user=request.user)
    comment_form = CommentForm(request.POST, instance=comment)
    if comment_form.is_valid():
        comment = comment_form.save(commit=False)
        comment.save(notify=True)
    return HttpResponseRedirect(reverse("proposal_detail", args=[event_url, pk]))


@login_required(login_url='../../../../../accounts/login/')
def vote_proposal(request, event_url, pk, vote):
    proposal = TalkProposal.objects.get(pk=pk)
    exits_vote = Vote.objects.get_for_user(proposal, request.user)
    if not exits_vote and vote in ("1", "0"):
        Vote.objects.record_vote(proposal, request.user, 1 if vote == '1' else -1)
    return proposal_detail(request, event_url, pk)


@login_required(login_url='../../../../../accounts/login/')
def cancel_vote(request, event_url, pk):
    proposal = TalkProposal.objects.get(pk=pk)
    vote = Vote.objects.get_for_user(proposal, request.user)
    if vote:
        vote.delete()
    return proposal_detail(request, event_url, pk)


@login_required(login_url='../../accounts/login/')
def confirm_schedule(request, event_url):
    event = Event.objects.get(url__iexact=event_url)
    event.schedule_confirm = True
    event.save()
    return schedule(request, event_url)


def reports(request, event_url):
    return render(request, 'reports/dashboard.html', update_event_info(event_url))


def generic_registration(request, event_url, registration_model, registration_form, msg_success, msg_error, template):
    event = Event.objects.get(url__iexact=event_url)

    if not event.registration_is_open:
        return render(request, 'registration/closed-registration.html', update_event_info(event_url))

    errors = []
    user, eventoLUser, attendee = None, None, None
    user_form = UserRegistrationForm(request.POST or None)

    if request.POST:
        eventoLUser_form = EventoLUserRegistrationForm(request.POST)
        registration_form = registration_form(request.POST)
        forms = [user_form, eventoLUser_form, registration_form]
        if user_form.is_valid():
            try:
                user = user_form.save()
                if eventoLUser_form.is_valid():
                    eventoLUser = eventoLUser_form.save()
                    eventoLUser.user = user
                    eventoLUser.save()
                    if registration_form.is_valid():
                        registration = registration_form.save()
                        registration.eventolUser = eventoLUser
                        registration.save()
                        messages.success(request, msg_success)
                        return HttpResponseRedirect('/event/' + event_url)
            except Exception:
                pass
        if user is not None:
            User.delete(user)
        if eventoLUser is not None:
            EventoLUser.delete(eventoLUser)
        if registration is not None:
            registration_model.delete(attendee)
        messages.error(request, msg_error)
        errors = get_forms_errors(forms)

    else:
        eventoLUser = EventoLUser(event=event)
        registration = registration_model(eventolUser=eventoLUser)
        eventoLUser_form = EventoLUserRegistrationForm(instance=eventoLUser)
        registration_form = registration_form(instance=attendee)
        forms = [user_form, eventoLUser_form, registration_form]

    return render(request,
                  template,
                  update_event_info(event_url, {'forms': forms, 'errors': errors, 'multipart': False}))


def registration(request, event_url):
    msg_success = _("We've sent you an email with the confirmation link. Please click or "
                    "copy and paste it in your browser to confirm the registration.")
    msg_error = _("The attendee hasn't been registered successfully (check form errors)")
    template = 'registration/attendee-registration.html'
    return generic_registration(request, event_url, Attendee,
                                AttendeeRegistrationForm, msg_success, msg_error, template)


def installer_registration(request, event_url):
    msg_success = _("You've been registered successfully!")
    msg_error = _("You haven't been registered successfully (check form errors)")
    template = 'registration/installer-registration.html'
    return generic_registration(request, event_url, Installer,
                                InstallerRegistrationForm, msg_success, msg_error, template)


def collaborator_registration(request, event_url):
    msg_success = _("You've been registered successfully!")
    msg_error = _("You haven't been registered successfully (check form errors)")
    template = 'registration/collaborator-registration.html'
    return generic_registration(request, event_url, Collaborator,
                                CollaboratorRegistrationForm, msg_success, msg_error, template)
