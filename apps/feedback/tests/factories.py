import factory

from apps.clubs.tests.factories import ClubFactory
from apps.feedback.models import FeedbackAnswer, FeedbackForm, FeedbackQuestion, FeedbackResponse
from apps.students.tests.factories import StudentFactory


class FeedbackFormFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FeedbackForm

    club = factory.SubFactory(ClubFactory)
    name = "Опрос после пробной"
    is_active = True


class FeedbackQuestionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FeedbackQuestion

    club = factory.LazyAttribute(lambda o: o.form.club)
    form = factory.SubFactory(FeedbackFormFactory)
    question_type = "rating"
    text = "Оцените тренировку от 1 до 5"
    order = factory.Sequence(lambda n: n + 1)
    is_required = False


class FeedbackResponseFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FeedbackResponse

    club = factory.LazyAttribute(lambda o: o.form.club)
    form = factory.SubFactory(FeedbackFormFactory)
    student = factory.SubFactory(StudentFactory)


class FeedbackAnswerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FeedbackAnswer

    club = factory.LazyAttribute(lambda o: o.response.club)
    response = factory.SubFactory(FeedbackResponseFactory)
    question = factory.SubFactory(FeedbackQuestionFactory)
    rating_value = 4
    text_value = ""
